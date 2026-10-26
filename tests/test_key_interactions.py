"""Key 创建与 CC Switch 导入的请求预算、命名及失效边界；仅合成凭据。"""
import copy
import unittest
from contextlib import ExitStack
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from integrations.configuration import read_json, write_json
from integrations.contracts import Feature
from integrations.providers import Credentials, CredentialSession, HttpResponse, ProviderFailure, RequestContext
from integrations.router import Router
from integrations.service import binding_of, create_token, resolve_export
from tests.test_stage_c import Hooks, provider, sub2
from tests import test_stage_d as stage_d


class KeyInteractionTests(unittest.TestCase):
    def setUp(self):
        # 复用既有合成远端，但不继承测试类，避免重复计数旧用例。
        fixture = stage_d.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.site, self.remote, self.runtime = fixture.site, fixture.remote, fixture.runtime
        self.folder, self.config = fixture.folder, fixture.config
        import server
        self.server = server
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in (
            (server, "TOKEN_SESSIONS", {}), (server, "TOKEN_REVISIONS", {}),
            (server, "DATA_DIR", self.folder / "data"),
            (server, "ACCOUNTS_FILE", self.folder / "accounts.json"),
        ):
            stack.enter_context(patch.object(target, name, value))
        stack.enter_context(patch.object(server.collector, "integration_runtime", return_value=self.runtime))
        # 明确写死直连：不写的话会按默认去读本机系统代理，换台机器结果就不一样，
        # 且会和直接调用 token_session(site) 时的默认空代理对不上，短会话缓存会误判为失效。
        stack.enter_context(patch.object(server, "ensure_settings", return_value={
            "proxy": "", "proxy_mode": "direct",
            "connection_policy":{"mode":"api_only","compat_enabled":False}}))
        stack.enter_context(patch.object(server, "load_sites", return_value=[self.site]))
        stack.enter_context(patch.object(server, "find_site", return_value=self.site))
        stack.enter_context(patch.object(server, "ccswitch_info", return_value={"api_format_ok":True}))
        stack.enter_context(patch.object(server, "log_line"))
        write_json(server.ACCOUNTS_FILE, {"accounts":[{"site":self.site["id"],"tokens":[],"ok":True,
                                                     "binding_ref":binding_of(self.site)}]})

    def export(self, **changes):
        body={"site":self.site["id"],"app":"codex","model":"raw-model","group":"default",
              "token_id":3,"create":False,"with_usage":True}
        body.update(changes)
        result,status=self.server.export_ccswitch(body)
        self.assertEqual(status,200,result)
        return result

    def test_cold_create_and_repeated_create_request_counts(self):
        for name, expected in (("first",4),("second",3)):
            before=len(self.remote.calls)
            with self.server.token_session(self.site) as router:
                self.assertEqual(router.api.read_retries,0)
                token=create_token(router,self.site,name=name,group="default")
            self.assertTrue(token["key"])
            self.assertEqual(len(self.remote.calls)-before,expected)
        self.assertEqual(sum(path=="/api/user/self" for _,path,_,_ in self.remote.calls),1)

    def test_existing_export_with_usage_then_repeated_export_counts(self):
        self.remote.rows=[{"id":3,"name":"actual-key","group":"default"}]
        for expected in (4,3):
            before=len(self.remote.calls)
            result=self.export(key_name="ignored-request-name")
            self.assertTrue(result["usage"])
            self.assertEqual(result["token_name"],"actual-key")
            self.assertEqual(result["token_id"],3)
            self.assertEqual(len(self.remote.calls)-before,expected)
        # 每次仍到远端核实列表、读取明文及实际单位，不复用旧 Key 列表或明文。
        for path in ("/api/token/","/api/token/3/key","/api/status"):
            self.assertEqual(sum(p==path for _,p,_,_ in self.remote.calls),2)

    def test_create_then_export_reuses_short_session_token_cache(self):
        # 创建接口和随后导入接口共用 120 秒短会话时，导入不应再次分页列表或 reveal。
        before = len(self.remote.calls)
        with self.server.token_session(self.site) as router:
            operation = self.server.integration_service._interaction_state(router)
            token = create_token(router, self.site, name="just-created", group="default",
                                 _operation=operation)
            created_calls = len(self.remote.calls) - before
            result = resolve_export(router, self.site, group="default", token_id=token["id"],
                                    create=False, model="raw-model", _operation=operation)
        self.assertEqual(created_calls, 4)
        self.assertEqual(len(self.remote.calls) - before, created_calls)
        self.assertEqual(result["token_id"], token["id"])
        self.assertEqual(result["token_name"], "just-created")
        self.assertEqual(result["key"], "sk-synthetic-full-model-key")

    def test_updated_created_key_rechecks_real_name_status_and_group(self):
        request=self.remote.request
        def remote(method,url,**kwargs):
            path=urlsplit(url).path
            if method=="GET" and path.startswith("/api/token/") and path.rsplit("/",1)[-1].isdigit():
                self.remote.calls.append((method,path,dict(kwargs["headers"]),kwargs["body"]))
                row=next(row for row in self.remote.rows if str(row["id"])==path.rsplit("/",1)[-1])
                return HttpResponse(200,{"success":True,"data":{"cross_group_retry":False,**copy.deepcopy(row)}})
            if method=="PUT" and path=="/api/token/":
                self.remote.calls.append((method,path,dict(kwargs["headers"]),kwargs["body"]))
                row=next(row for row in self.remote.rows if str(row["id"])==str(kwargs["body"]["id"]))
                row.update(kwargs["body"])
                return HttpResponse(200,{"success":True,"data":{}})
            return request(method,url,**kwargs)
        with patch.object(self.remote,"request",side_effect=remote):
            for changes in ({"name":"真实更新名"},{"status":2},{"group":"other"}):
                with self.subTest(changes=changes), self.server.token_session(self.site) as router:
                    token=create_token(router,self.site,name="cached-name",group="default")
                    self.assertIn(str(token["id"]),router.interactive_token_cache)
                    self.assertTrue(router.fetch(Feature.KEYS_UPDATE,id=token["id"],changes=changes).ok)
                    self.assertFalse(router.interactive_token_cache)
                    before=len(self.remote.calls)
                    if "name" in changes:
                        result=resolve_export(router,self.site,group="default",token_id=token["id"],
                                              create=False,model="raw-model")
                        self.assertEqual(result["token_name"],changes["name"])
                    else:
                        with self.assertRaises(ProviderFailure):
                            resolve_export(router,self.site,group="default",token_id=token["id"],
                                           create=False,model="raw-model")
                    self.assertTrue(any(path=="/api/token/" for _,path,_,_ in self.remote.calls[before:]))

    def test_failed_read_or_delete_result_clears_created_key_cache(self):
        for mode in ("failed_read","unknown_delete","confirmed_delete"):
            with self.subTest(mode=mode), self.server.token_session(self.site) as router:
                self.remote.reveal_denied=False
                self.remote.fail_after_write=False
                token=create_token(router,self.site,name="cached-name",group="default")
                self.assertIn(str(token["id"]),router.interactive_token_cache)
                self.remote.reveal_denied=mode=="failed_read"
                self.remote.fail_after_write=mode=="unknown_delete"
                result=router.fetch(Feature.KEYS_REVEAL if mode=="failed_read" else Feature.KEYS_DELETE,id=token["id"])
                self.assertEqual(result.ok,mode=="confirmed_delete")
                self.assertFalse(router.interactive_token_cache)
                self.remote.reveal_denied=False
                self.remote.fail_after_write=False

    def test_auto_create_export_counts_and_masked_account_projection(self):
        result=self.export(token_id=None,create=True,key_name="my-key")
        self.assertEqual(len(self.remote.calls),5)
        self.assertEqual(self.remote.writes,1)
        self.assertEqual(result["token_name"],"my-key")
        self.assertEqual(result["account"]["token_count"],1)
        disk=self.server.ACCOUNTS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-full-model-key",disk)
        self.assertTrue(all("key" not in token for token in result["account"]["tokens"]))
        journal=read_json(self.folder/"data"/"write_operations.json")
        self.assertEqual(journal["operations"][0]["status"],"confirmed")

    def test_create_then_import_reuses_same_short_session_token(self):
        """创建接口与随后导入接口共用短会话时，不应再次读列表或 reveal。"""
        with self.server.token_session(self.site) as router:
            token = create_token(router, self.site, name="created-before-import", group="default")
        before = len(self.remote.calls)
        result = self.export(token_id=token["id"], create=False, with_usage=False)
        self.assertEqual(result["token_id"], token["id"])
        self.assertEqual(result["token_name"], "created-before-import")
        # profile 也在 120 秒交互会话中缓存；导入只需本地生成深链。
        self.assertEqual(len(self.remote.calls) - before, 0)

    def test_expired_short_session_does_not_reuse_created_token_cache(self):
        """短会话过期后必须重新远端核验，避免跨会话复用明文 Key。"""
        with self.server.token_session(self.site) as router:
            token = create_token(router, self.site, name="created-expiring", group="default")
        for entry in self.server.TOKEN_SESSIONS.values():
            entry["expires"] = 1
        before = len(self.remote.calls)
        result = self.export(token_id=token["id"], create=False, with_usage=False)
        self.assertEqual(result["token_id"], token["id"])
        # 新会话需要 profile、列表和明文 reveal；旧缓存不应绕过这些校验。
        self.assertEqual(len(self.remote.calls) - before, 3)

    def test_link_name_uses_real_key_name_and_roundtrips_special_characters(self):
        self.site["name"]="云端网 中文&/?#🦊"
        write_json(self.config,{"sites":[self.site]})
        actual="真实 Key &/?#🔑"
        self.remote.rows=[{"id":3,"name":actual,"group":"default"}]
        result=self.export(model="模型 &/?#🚀",key_name="cc-switch")
        query=parse_qs(urlsplit(result["link"]).query)
        self.assertEqual(query["name"],[self.site["name"]+"-"+actual+"-模型 &/?#🚀"])
        self.assertEqual(self.remote.rows[0]["name"],actual)
        self.assertEqual(self.remote.writes,0)

    def test_blank_key_name_has_explicit_fallback(self):
        for name in ("","   ",None):
            with self.subTest(name=name):
                self.remote.rows=[{"id":3,"name":name,"group":"default"}]
                result=self.export(with_usage=False)
                self.assertEqual(parse_qs(urlsplit(result["link"]).query)["name"],
                                 ["Fixture-未命名 Key-raw-model"])

    def test_confirmed_create_plaintext_and_real_returned_name_avoid_reveal(self):
        request=self.remote.request
        def remote(method,url,**kwargs):
            response=request(method,url,**kwargs)
            if method=="POST" and urlsplit(url).path=="/api/token/":
                response.payload["data"].update(key="synthetic-created",name="真实回包名称")
            return response
        with patch.object(self.remote,"request",side_effect=remote):
            result=self.export(token_id=None,create=True,key_name="request-name")
        self.assertEqual(len(self.remote.calls),4)
        self.assertFalse(any(path.endswith("/key") for _,path,_,_ in self.remote.calls))
        self.assertEqual(parse_qs(urlsplit(result["link"]).query)["name"],
                         ["Fixture-真实回包名称-raw-model"])
        self.assertEqual(self.remote.rows[0]["name"],"request-name")

    def test_masked_or_invalid_create_key_still_reveals_once(self):
        request=self.remote.request
        for returned in ("sk-mask***","",None):
            with self.subTest(returned=returned):
                before=len(self.remote.calls)
                def remote(method,url,**kwargs):
                    response=request(method,url,**kwargs)
                    if method=="POST" and urlsplit(url).path=="/api/token/":
                        response.payload["data"]["key"]=returned
                    return response
                with patch.object(self.remote,"request",side_effect=remote), self.server.token_session(self.site) as router:
                    token=create_token(router,self.site,name="fallback",group="default")
                self.assertEqual(token["key"],"sk-synthetic-full-model-key")
                calls=self.remote.calls[before:]
                self.assertEqual(sum(path.endswith("/key") for _,path,_,_ in calls),1)

    def test_complete_multipage_before_is_reused_but_no_id_create_still_reconciles(self):
        self.remote.rows=[{"id":i,"name":"old","group":"other"} for i in range(1,102)]
        request=self.remote.request
        def remote(method,url,**kwargs):
            path=urlsplit(url).path
            if path=="/api/token/":
                self.remote.calls.append((method,path,dict(kwargs["headers"]),kwargs["body"]))
                if method=="GET":
                    page=int(parse_qs(urlsplit(url).query)["p"][0])
                    rows=copy.deepcopy(self.remote.rows[(page-1)*100:page*100])
                    return HttpResponse(200,{"success":True,"data":{"items":rows,"total":len(self.remote.rows)}})
                self.remote.writes+=1
                self.remote.rows.append({"id":102,**kwargs["body"]})
                return HttpResponse(200,{"success":True,"data":{}})
            return request(method,url,**kwargs)
        with patch.object(self.remote,"request",side_effect=remote):
            result=self.export(token_id=None,create=True,with_usage=False)
        self.assertEqual(result["token_id"],102)
        self.assertEqual(len(self.remote.calls),7)
        self.assertEqual(self.remote.writes,1)
        journal=read_json(self.folder/"data"/"write_operations.json")
        self.assertEqual(len(journal["operations"][0]["evidence"]["before_ids"]),101)

    def test_unknown_create_is_single_write_after_session_is_discarded(self):
        self.remote.fail_after_write=True
        for attempt in range(2):
            with self.assertRaises(ProviderFailure), self.server.token_session(self.site) as router:
                create_token(router,self.site,name="same-request",group="default")
            self.assertEqual(self.remote.writes,1)

    def test_expiry_errors_and_configuration_changes_invalidate_short_session(self):
        with self.server.token_session(self.site) as first:
            self.assertTrue(first.fetch(Feature.PROFILE).ok)
            self.assertTrue(first.interactive_profile_cache)
        for entry in self.server.TOKEN_SESSIONS.values():
            entry["expires"]=1
        with self.server.token_session(self.site) as second:
            self.assertIsNot(second,first)
            self.assertTrue(second.fetch(Feature.PROFILE).ok)
        self.assertEqual(sum(path=="/api/user/self" for _,path,_,_ in self.remote.calls),2)
        self.remote.reveal_denied=True
        with self.server.token_session(self.site) as current:
            self.assertFalse(current.fetch(Feature.KEYS_REVEAL,id=3).ok)
            self.assertFalse(current.interactive_profile_cache)
        for change,proxy in (({"auth":{"management_token":"changed"}},""),
                             ({"api_prefix":"/changed"},""),({},"http://proxy.invalid:80")):
            with self.subTest(change=change,proxy=proxy):
                changed={**self.site,**change}
                with self.server.token_session(changed,proxy) as other:
                    self.assertIsNot(other,current)
                    self.assertFalse(other.interactive_profile_cache)

    def test_failed_usage_does_not_block_key_import_or_claim_support(self):
        self.remote.rows=[{"id":3,"name":"actual-key","group":"default"}]
        request=self.remote.request
        def remote(method,url,**kwargs):
            if urlsplit(url).path=="/api/status":
                self.assertLessEqual(kwargs["timeout"],12)
                raise TimeoutError()
            return request(method,url,**kwargs)
        with patch.object(self.remote,"request",side_effect=remote):
            result=self.export()
        self.assertFalse(result["usage"])
        self.assertNotIn("usageScript",parse_qs(urlsplit(result["link"]).query))
        self.assertTrue(any("已跳过" in warning for warning in result["warn"]))

    def test_config_change_during_optional_usage_never_writes_old_key_into_new_account(self):
        request=self.remote.request
        saved={}
        def remote(method,url,**kwargs):
            response=request(method,url,**kwargs)
            if urlsplit(url).path=="/api/status":
                # 创建已确认后用户切换账户；后续写回只能保留新绑定的本地快照。
                self.site["auth"]={"management_token":"replacement-account-token","account_id":8}
                write_json(self.config,{"sites":[self.site]})
                saved.update(accounts=[{"site":self.site["id"],"user_id":8,
                    "binding_ref":binding_of(self.site),"tokens":[{"id":77,"name":"new-account-key"}]}])
                write_json(self.server.ACCOUNTS_FILE,saved)
            return response
        with patch.object(self.remote,"request",side_effect=remote):
            result=self.export(token_id=None,create=True,key_name="old-account-key")
        self.assertTrue(result["created"])
        self.assertIsNone(result["account"])
        self.assertEqual(read_json(self.server.ACCOUNTS_FILE),saved)
        self.assertNotIn("old-account-key",self.server.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertTrue(any("绑定已变更" in warning for warning in result["warn"]))

    def test_disabled_expired_wrong_group_or_model_key_is_not_revealed(self):
        for changes in ({"status":2},{"expired_time":1},{"group":"other"},
                        {"model_limits_enabled":True,"model_limits":"other-model"},
                        {"expires_at":"2000-01-01T00:00:00Z"}, {"expires_at":"bad"}):
            with self.subTest(changes=changes):
                self.remote.rows=[{"id":3,"name":"unusable","group":"default",**changes}]
                before=len(self.remote.calls)
                with self.assertRaises(ProviderFailure), self.server.token_session(self.site) as router:
                    resolve_export(router,self.site,group="default",token_id=3,create=False,model="raw-model")
                self.assertFalse(any(path.endswith("/key") for _,path,_,_ in self.remote.calls[before:]))


class Sub2KeyInteractionTests(unittest.TestCase):
    SITE={"id":"fixture","name":"Fixture","type":"sub2api","base_url":"https://fixture.invalid"}

    def instance(self, rows=None, credentials=None):
        def remote(call):
            path=urlsplit(call["url"]).path
            if path.endswith("/user/profile"):
                return sub2({"id":7})
            if path.endswith("/keys") and call["method"]=="POST":
                return sub2({"id":4,"name":call["body"]["name"],"group_id":call["body"]["group_id"],
                             "key":"sk-sub2-created"})
            if path.endswith("/keys"):
                return sub2({"items":rows or [],"total":len(rows or [])})
            if "/keys/" in path and path.rsplit("/",1)[-1].isdigit():
                return sub2({"id":int(path.rsplit("/",1)[-1]),"key":"sk-sub2-existing"})
            raise AssertionError("Unexpected route "+path)
        session=CredentialSession(credentials or Credentials(access_token="synthetic-panel",account_id=7),
                                  save_rotation=lambda expected,replacement:True)
        api,transport=provider("sub2api",remote,session=session,
                               write_hooks=Hooks())
        router=Router(api)
        router.interactive_profile_cache={}
        router.interactive_token_cache={}
        return router,transport

    def test_explicit_numeric_group_id_skips_name_lookup_and_creates_once(self):
        router,transport=self.instance()
        token=create_token(router,self.SITE,name="actual-name",group="展示分组",group_id=8)
        self.assertEqual(token["group"],"展示分组")
        self.assertEqual(token["group_id"],8)
        self.assertEqual(len(transport.calls),2)
        self.assertEqual(transport.calls[-1]["body"]["group_id"],8)
        self.assertFalse(any("/groups/" in call["url"] for call in transport.calls))

    def test_explicit_id_matches_real_group_even_when_display_name_changed(self):
        router,transport=self.instance([{"id":3,"name":"real-name","group_id":8,"group":{"name":"旧名称"}}])
        result=resolve_export(router,self.SITE,group="展示分组",group_id=8,token_id=3,create=False,model="m")
        self.assertEqual(result["token_name"],"real-name")
        self.assertEqual(len(transport.calls),3)

    def test_token_rotation_rejects_created_key_cache_and_reads_current_remote_record(self):
        router,transport=self.instance([{"id":4,"name":"续期后真实名称","group_id":8}],
            credentials=Credentials(access_token="synthetic-panel",refresh_token="synthetic-refresh",account_id=7))
        token=create_token(router,self.SITE,name="cached-name",group="展示分组",group_id=8)
        session=router.api.session
        old=session.snapshot(router.api.config_revision)
        session.refresh(old,RequestContext(),lambda credentials:replace(credentials,access_token="rotated-panel"))
        before=len(transport.calls)
        result=resolve_export(router,self.SITE,group="展示分组",group_id=8,token_id=token["id"],create=False,model="m")
        self.assertEqual(result["token_name"],"续期后真实名称")
        self.assertEqual(result["key"],"sk-sub2-existing")
        self.assertEqual(len(transport.calls)-before,3)

    def test_invalid_or_contradictory_group_id_never_creates_or_reveals(self):
        for group,group_id in (("8",9),("展示分组",True),("展示分组",0),("展示分组","bad")):
            with self.subTest(group=group,group_id=group_id):
                router,transport=self.instance()
                with self.assertRaises(ProviderFailure):
                    create_token(router,self.SITE,name="actual",group=group,group_id=group_id)
                self.assertFalse(transport.calls)


if __name__=="__main__":
    unittest.main()
