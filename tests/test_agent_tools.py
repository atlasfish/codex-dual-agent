import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import sys
import httpx
import pytest
from dual_agent.config import Config, UserError
from dual_agent.hooks import handle
from dual_agent.ledger import Ledger
from dual_agent.provider import Relay
from dual_agent.skills import Skills
from dual_agent.tools import ToolRuntime
from dual_agent.worker import run_job


def python_command(source):
    if os.name == "nt":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + source.replace("'", "''") + "'"
    return shlex.join([sys.executable, "-c", source])


def chat_call(name, args, cid="call-1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
                        "extra_content": {"google": {"thought_signature": "opaque-signature"}}}]}}]}


def chat_final(text="done"):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


async def test_chat_full_tool_loop_keeps_vendor_fields(cfg, workspace):
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert {t['function']['name'] for t in body['tools']} >= {'shell', 'read_skill', 'web_search'}
            return httpx.Response(200, json=chat_call('shell', {'command': python_command('print("hello from shell")')}))
        previous, result = body['messages'][-2:]
        assert previous['tool_calls'][0]['extra_content']['google']['thought_signature'] == 'opaque-signature'
        assert 'hello from shell' in result['content']
        assert result['tool_call_id'] == 'call-1'
        return httpx.Response(200, json=chat_final())
    runtime = ToolRuntime(cfg, workspace)
    assert await Relay(cfg, httpx.MockTransport(respond), runtime).generate({'mode': 'gemini'}) == 'done'
    assert len(calls) == 2 and runtime.calls == 1


async def test_responses_preserves_reasoning_and_call_id(cfg, workspace):
    cfg = replace(cfg, protocol='responses')
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body['tools'][0]['type'] == 'function'
            return httpx.Response(200, json={'status': 'completed', 'output': [
                {'type': 'reasoning', 'id': 'r', 'summary': [], 'encrypted_content': 'opaque'},
                {'type': 'function_call', 'name': 'read_file', 'call_id': 'c', 'arguments': '{"path":"main.kt"}'}]})
        assert body['input'][-3]['encrypted_content'] == 'opaque'
        assert body['input'][-1]['type'] == 'function_call_output'
        assert body['input'][-1]['call_id'] == 'c'
        assert 'println' in body['input'][-1]['output']
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}]})
    assert await Relay(cfg, httpx.MockTransport(respond), ToolRuntime(cfg, workspace)).generate({'mode':'gemini'}) == 'ok'


async def test_repeated_call_id_does_not_repeat_side_effect(cfg, workspace):
    runtime = ToolRuntime(cfg, workspace)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=chat_call('list_skills', {})))
    with pytest.raises(UserError, match='repeated'):
        await Relay(cfg, transport, runtime).generate({'mode':'gemini'})
    assert runtime.calls == 1


async def test_budget_offers_final_answer_turn(cfg, workspace):
    cfg = replace(cfg, max_tool_rounds=1)
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            return httpx.Response(200, json=chat_call('list_skills', {}))
        assert body['tool_choice'] == 'none'
        return httpx.Response(200, json=chat_final())
    assert await Relay(cfg, httpx.MockTransport(respond), ToolRuntime(cfg, workspace)).generate({'mode':'gemini'}) == 'done'


async def test_budget_denies_next_call(cfg, workspace):
    cfg = replace(cfg, max_tool_calls=1)
    runtime = ToolRuntime(cfg, workspace)
    await runtime.call('list_skills', '{}')
    with pytest.raises(UserError, match='budget'):
        await runtime.call('list_skills', '{}')


async def test_skill_script_with_environment(cfg, workspace, monkeypatch):
    skill = workspace / '.agents/skills/server-status'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: server-status\ndescription: |\n  Query my server.\n---\nRun scripts/check.py using Python.\n', encoding='utf-8')
    (skill / 'scripts').mkdir()
    (skill / 'scripts/check.py').write_text('import os\nprint("authenticated" if os.getenv("SERVER_TOKEN") else "missing")\n')
    monkeypatch.setenv('SERVER_TOKEN', 'private-demo-token')
    runtime = ToolRuntime(cfg, workspace)
    catalog = json.loads(await runtime.call('list_skills', '{}'))
    entry = next(s for s in catalog['skills'] if s['name'] == 'server-status')
    result = json.loads(await runtime.call('read_skill', json.dumps({'skill_id': entry['id']})))
    assert 'scripts/check.py' in result['content']
    command = python_command('exec(open("scripts/check.py").read())')
    executed = json.loads(await runtime.call('shell', json.dumps({'command': command, 'cwd': result['directory']})))
    assert executed['exit_code'] == 0 and 'authenticated' in executed['output']
    assert 'private-demo-token' not in json.dumps(executed)


async def test_shell_is_intentionally_not_read_only(cfg, workspace):
    runtime = ToolRuntime(cfg, workspace)
    result = json.loads(await runtime.call('shell', json.dumps({'command': python_command('open("cache.txt", "w").write("cache")')})))
    assert result['exit_code'] == 0
    assert (workspace / 'cache.txt').read_text() == 'cache'
    assert not {'write_file', 'edit_file', 'apply_patch'} & runtime.names


async def test_shell_cancel_kills_process(cfg, workspace):
    runtime = ToolRuntime(cfg, workspace)
    pid_path = workspace / 'pid.txt'
    task = asyncio.create_task(runtime.call('shell', json.dumps({'command': python_command('import os,time; open("pid.txt","w").write(str(os.getpid())); time.sleep(30)')})))
    for _ in range(100):
        if pid_path.exists() and pid_path.read_text():
            break
        await asyncio.sleep(.02)
    assert pid_path.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def test_shell_timeout(cfg, workspace):
    runtime = ToolRuntime(replace(cfg, shell_timeout_seconds=1), workspace)
    result = json.loads(await runtime.call('shell', json.dumps({'command': python_command('import time;time.sleep(30)')})))
    assert result['timed_out'] is True


async def test_shell_output_bounded_and_redacted(cfg, workspace, monkeypatch):
    monkeypatch.setenv('SERVER_TOKEN', 'secret-auth-value')
    runtime = ToolRuntime(replace(cfg, max_tool_output_bytes=1024), workspace)
    raw = await runtime.call('shell', json.dumps({'command': python_command('import os; print(os.environ["SERVER_TOKEN"]);print("x"*10000)')}))
    assert len(raw.encode()) <= 1024 and 'secret-auth-value' not in raw
    assert json.loads(raw)['truncated']


async def test_http_rest_uses_local_auth_and_no_redirect(cfg, workspace, monkeypatch):
    monkeypatch.setenv('SERVER_TOKEN', 'local-private-token')
    def respond(req):
        assert req.method == 'POST'
        assert req.headers['Authorization'] == 'Bearer local-private-token'
        assert req.content == b'{"query":"status"}'
        return httpx.Response(200, json={'status': 'healthy', 'accidental_echo': 'local-private-token'})
    runtime = ToolRuntime(cfg, workspace, transport=httpx.MockTransport(respond))
    result = await runtime.call('http_request', json.dumps({'url':'http://127.0.0.1:8000/query','method':'POST',
                'headers':{'Authorization':'Bearer ${SERVER_TOKEN}'}, 'body':'{"query":"status"}'}))
    assert 'healthy' in result and 'local-private-token' not in result


async def test_http_redirect_not_followed(cfg, workspace):
    seen=[]
    def respond(req):
        seen.append(req)
        return httpx.Response(302, headers={'Location':'https://different.invalid'})
    runtime=ToolRuntime(cfg,workspace,transport=httpx.MockTransport(respond))
    result=json.loads(await runtime.call('http_request','{"url":"http://localhost/api"}'))
    assert result['status']==302 and len(seen)==1


async def test_search_default_and_failure(cfg, workspace):
    html='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fdoc">Actual source</a>'
    runtime=ToolRuntime(cfg,workspace,transport=httpx.MockTransport(lambda _:httpx.Response(200,text=html)))
    result=json.loads(await runtime.call('web_search','{"query":"some docs"}'))
    assert result['results'][0]['url']=='https://example.org/doc'
    runtime.transport=httpx.MockTransport(lambda _:httpx.Response(200,text='CAPTCHA challenge'))
    assert 'error' in json.loads(await runtime.call('web_search','{"query":"some docs"}'))


async def test_searxng_search(cfg,workspace):
    cfg=replace(cfg,search_backend='searxng',search_url='http://localhost/search')
    def respond(req):
        assert req.url.params['q']=='docs' and req.url.params['format']=='json'
        return httpx.Response(200,json={'results':[{'title':'Docs','url':'https://example.org','content':'Snippet'}]})
    runtime=ToolRuntime(cfg,workspace,transport=httpx.MockTransport(respond))
    assert json.loads(await runtime.call('web_search','{"query":"docs"}'))['results'][0]['snippet']=='Snippet'


async def test_search_script_argv_no_shell_interpolation(cfg,workspace):
    source='import sys,json; print(json.dumps([{"title":sys.argv[1],"url":"https://example.org"}]))'
    cfg=replace(cfg,search_backend='command',search_command=(sys.executable,'-c',source,'{query}'))
    runtime=ToolRuntime(cfg,workspace)
    query='hello ; echo unsafe > no.txt'
    result=json.loads(await runtime.call('web_search',json.dumps({'query':query})))
    assert result['results'][0]['title']==query and not (workspace/'no.txt').exists()


@pytest.mark.parametrize('args', ['[]','{"command":123}','{"command":"ok","unknown":true}','bad-json'])
async def test_bad_tool_arguments(cfg,workspace,args):
    assert 'error' in json.loads(await ToolRuntime(cfg,workspace).call('shell',args))


async def test_capability_switches_are_not_a_sandbox(cfg,workspace):
    runtime=ToolRuntime(replace(cfg,shell_enabled=False,network_enabled=False,skills_enabled=False),workspace)
    assert runtime.names=={'read_file'}
    assert 'error' in json.loads(await runtime.call('shell','{"command":"echo test"}'))


@pytest.mark.parametrize('key,value', [('max_tool_calls',0),('shell_enabled','yes'),('search_backend','imaginary'),('skill_roots','/tmp')])
def test_tool_config_validation(cfg,key,value):
    with pytest.raises(UserError):
        replace(cfg,**{key:value}).validate()


async def test_audit_stores_only_metadata(cfg,workspace,make_turn):
    ledger,turn=make_turn()
    job=ledger.job_for_turn(turn['id'])
    runtime=ToolRuntime(cfg,workspace,audit=lambda *args:ledger.audit_tool(job['id'],*args))
    await runtime.call('read_file','{"path":"main.kt"}')
    rows=ledger.tool_usage(job['id'])
    assert rows[0]['name']=='read_file'
    assert 'println' not in json.dumps(rows)


def test_completed_job_releases_duplicate_request(make_turn):
    ledger,turn=make_turn(files=('main.kt',))
    job=ledger.job_for_turn(turn['id'])
    assert 'println' in job['payload']
    ledger.claim(job['id']);ledger.finish(job['id'],'answer')
    assert json.loads(ledger.job(job['id'])['payload'])=={}
    assert 'println' in ledger.by_token(turn['token'])['snapshot']


def test_host_shell_not_denied_but_direct_editor_is(cfg,workspace):
    base={'cwd':str(workspace),'session_id':'s','turn_id':'t'}
    handle({**base,'hook_event_name':'UserPromptSubmit','prompt':'@gemini check'},cfg,launcher=lambda _:None)
    assert handle({**base,'hook_event_name':'PreToolUse','tool_name':'shell_command'},cfg)=={}
    assert handle({**base,'hook_event_name':'PreToolUse','tool_name':'apply_patch'},cfg)['hookSpecificOutput']['permissionDecision']=='deny'


def test_portable_snapshot_read(cfg,workspace):
    from dual_agent.context import portable_read
    text,digest=portable_read(workspace,'main.kt',1024)
    assert 'println' in text and len(digest)==64
    with pytest.raises(UserError):
        portable_read(workspace,'main.kt',1)


async def test_read_file_pagination_keeps_all_bytes(cfg,workspace):
    content='中文abc\\"\n'*1000
    (workspace/'large.txt').write_text(content,encoding='utf-8')
    runtime=ToolRuntime(replace(cfg,max_tool_output_bytes=1024,max_tool_calls=256),workspace)
    chunks=[];offset=0
    while True:
        raw=await runtime.call('read_file',json.dumps({'path':'large.txt','offset':offset}))
        assert len(raw.encode()) <= 1024
        result=json.loads(raw);chunks.append(result['content']);offset=result['next_offset']
        if not result['truncated']:
            break
    assert ''.join(chunks)==content


async def test_worker_real_loopback_tool_roundtrip(cfg,workspace,make_turn):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if len(requests)==1:
                response=chat_call('shell',{'command':python_command('print("worker-tool-roundtrip")')})
            else:
                response=chat_final('worker finished')
            raw=json.dumps(response).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    ledger,turn=make_turn();job=ledger.job_for_turn(turn['id'])
    try:
        await run_job(replace(cfg,base_url=f'http://127.0.0.1:{server.server_port}/v1'),job['id'])
        result=ledger.collect(turn['token'])
        assert result['state']=='succeeded' and result['text']=='worker finished'
        assert len(requests)==2 and 'tools' in requests[0]
        assert 'worker-tool-roundtrip' in requests[1]['messages'][-1]['content']
        assert result['tool_usage'][0]['name']=='shell'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
