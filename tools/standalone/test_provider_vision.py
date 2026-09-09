#!/usr/bin/env python3
"""Offline integration contracts; never establishes real-provider acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from collections import OrderedDict
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / 'Android/appliance/runtime' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


provider = module('provider', 'provider-launcher.py')
glm_adapter = module('glm_adapter', 'glm-chat-adapter.py')


def settings(**values):
    return {'provider': 'openai', 'apiKey': 'fixture-main-' + 'secret', **values}


def config(values):
    args, env, catalog = provider.launch_config(values, Path('/tmp/catalog.json'))
    return dict(item.split('=', 1) for item in args[1::2]), env, catalog


class ProviderTests(unittest.TestCase):
    def test_account_apps_disabled_without_disabling_local_mcp(self):
        for name in ('glm', 'minimax', 'openai'):
            with self.subTest(provider=name):
                values, _, _ = config(settings(provider=name, mcpServers=[
                    {'name': 'local', 'url': 'http://127.0.0.1:3111'}
                ]))
                self.assertIs(json.loads(values['features.apps']), False)
                self.assertEqual(json.loads(values['mcp_servers.user_local.url']),
                                 'http://127.0.0.1:3111')

    def test_mobile_experience_instructions_apply_to_every_main_provider(self):
        expected = (provider.BASE_INSTRUCTIONS.parent / 'mobile-experience-instructions.md').read_text(encoding='utf-8')
        for name in ('glm', 'minimax', 'openai'):
            with self.subTest(provider=name):
                values, _, catalog = config(settings(provider=name))
                self.assertEqual(json.loads(values['developer_instructions']), expected)
                self.assertIn('ace_recall', expected)
                self.assertIn('cheby-image:', expected)
                self.assertIn('image(block)', expected)
                self.assertIn('shot.structuredContent', expected)
                self.assertIn('local `phonebridge` MCP only', expected)
                self.assertIn('checkout_preapproved_by_owner: true', expected)
                self.assertIn('confirmation_required', expected)
                self.assertIn('preserve explicit stop-before-submission', expected.lower())
                self.assertNotIn('checkout_preapproved_by_owner', catalog['models'][0]['base_instructions'])
                self.assertNotIn('Browser launching is disabled', catalog['models'][0]['base_instructions'])
                self.assertNotIn('Routine in-scope operations need no additional confirmation', catalog['models'][0]['base_instructions'])
                self.assertIn('no app-category, keyword, or missing-semantics', expected)
                self.assertIn('final checkout control has no readable semantics', expected)

    def test_independent_vision_mcp_is_not_registered(self):
        values, env, _ = config(settings())
        self.assertFalse(any(name.startswith('mcp_servers.cheby_vision') for name in values))
        self.assertFalse(any(name.startswith('CHEBY_VISION_') for name in env))

    def test_preserves_pinned_upstream_instructions_and_rejects_corruption(self):
        for name in ('glm', 'minimax', 'openai'):
            _, _, catalog = config(settings(provider=name))
            self.assertEqual(catalog['models'][0]['base_instructions'].encode(),
                             provider.BASE_INSTRUCTIONS.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prompt.md'
            path.write_text('Modified or empty upstream instructions')
            with patch.object(provider, 'BASE_INSTRUCTIONS', path):
                with self.assertRaises(ValueError):
                    config(settings())
            path.unlink()
            with patch.object(provider, 'BASE_INSTRUCTIONS', path):
                with self.assertRaises(OSError):
                    config(settings())

    def test_only_authorized_models_and_provider_defaults(self):
        for name, model, base, effort in [
            ('glm', 'glm-5.3-flash', 'https://api.z.ai/api/paas/v4', 'low'),
            ('minimax', 'MiniMax-M3', 'https://api.minimaxi.com/v1', 'medium'),
            ('openai', 'gpt-5.6-sol', 'https://api.openai.com/v1', 'high'),
        ]:
            values, env, catalog = config(settings(provider=name))
            self.assertEqual(json.loads(values['model']), model)
            self.assertEqual(json.loads(values['model_reasoning_effort']), effort)
            self.assertEqual(catalog['models'][0]['input_modalities'], ['text', 'image'])
            self.assertNotIn('fixture-main-secret', json.dumps([values, catalog]))
            if name == 'openai':
                self.assertNotIn('model_provider', values)
                self.assertNotIn('model_catalog_json', values)
                self.assertNotIn('CHEBY_MODEL_API_KEY', env)
            else:
                self.assertEqual(json.loads(values['model_providers.cheby_user.base_url']), base)
                self.assertEqual(json.loads(values['model_providers.cheby_user.wire_api']), 'responses')
                self.assertEqual(env['CHEBY_MODEL_API_KEY'], 'fixture-main-secret')
        for name in ('deepseek', None, [], ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config(settings(provider=name))

    def test_reasoning_config_is_respected_and_bounded(self):
        supported = {
            'glm': ('low', 'high', 'max'),
            'minimax': ('low', 'medium', 'high', 'xhigh', 'max'),
            'openai': ('none', 'low', 'medium', 'high', 'xhigh', 'max'),
        }
        for name, efforts in supported.items():
            for effort in efforts:
                values, _, _ = config(settings(provider=name, reasoningEffort=effort))
                self.assertEqual(json.loads(values['model_reasoning_effort']), effort)
        for effort in ('priority', True, {}, ['high']):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                config(settings(reasoningEffort=effort))
        with self.assertRaises(ValueError):
            config(settings(provider='glm', reasoningEffort='xhigh'))
        with self.assertRaises(ValueError):
            config(settings(provider='minimax', reasoningEffort='ultra'))

    def test_credential_shapes_rejected(self):
        for key in (None, '', 'a b', 'a\nb', 'a\x00b', 123, 'ключ', 'x' * 8193):
            with self.subTest(key=type(key)), self.assertRaises(ValueError):
                config(settings(provider='glm', apiKey=key))

    def test_openai_subscription_login_does_not_require_or_forward_api_key(self):
        values, env, _ = config({'provider': 'openai'})
        self.assertEqual(json.loads(values['model']), 'gpt-5.6-sol')
        self.assertNotIn('model_provider', values)
        self.assertEqual({}, env)
        with self.assertRaisesRegex(ValueError, 'official service'):
            config({'provider': 'openai', 'baseUrl': 'https://example.test/v1'})

    def test_endpoint_transport_and_injection(self):
        for url in ('http://example.com', 'https://a:b@example.com', 'https://example.com?token=x', 'https://example.com#x', 'https://example.com:abc', 'https://example.com:0', 'https://example.com\n/x', 'https://example.com\\@other', [], None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                provider.endpoint(url)
        for url in ('https://example.com/v1', 'http://127.0.0.1:1234', 'http://localhost:1234', 'http://[::1]:1234'):
            self.assertEqual(provider.endpoint(url + '/'), url)

    def test_schema_types(self):
        for value in (None, [], '', True):
            with self.assertRaises(ValueError):
                config(value)
        for values in ({'mcpServers': {}}, {'mcpServers': [None]}, {'mcpServers': [{}]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(settings(**values))

    def test_legacy_independent_vision_fields_are_rejected(self):
        for name in ('visionEnabled', 'visionBaseUrl', 'visionKey', 'clearVisionKey'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Independent vision MCP'):
                config(settings(**{name: False if name.endswith('Enabled') else 'legacy'}))

    def test_mcp_names_and_duplicate_disabled_servers(self):
        for name in ('phonebridge', 'PhoneBridge', 'cheby_vision', 'user_test', 'a.b', '汉字', '', 1):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config(settings(mcpServers=[{'name': name, 'url': 'http://localhost:1234'}]))
        with self.assertRaises(ValueError):
            config(settings(mcpServers=[{'name': 'abc', 'enabled': False}, {'name': 'ABC', 'url': 'http://localhost:1234'}]))

    def test_remote_mcp_requires_specific_boolean_authorization(self):
        for authorized in (False, 'true', 1):
            with self.subTest(authorized=authorized), self.assertRaises(ValueError):
                config(settings(mcpServers=[{'name': 'docs', 'url': 'https://example.com/mcp', 'remoteAuthorized': authorized}]))
        values, env, _ = config(settings(mcpServers=[{'name': 'docs', 'url': 'https://example.com/mcp', 'remoteAuthorized': True, 'token': 'fixture-mcp-secret'}]))
        self.assertEqual(json.loads(values['mcp_servers.user_docs.bearer_token_env_var']), 'CHEBY_MCP_TOKEN_0')
        self.assertEqual(env['CHEBY_MCP_TOKEN_0'], 'fixture-mcp-secret')
        self.assertNotIn('fixture-mcp-secret', str(values))
        config(settings(mcpServers=[{'name': 'local', 'url': 'http://127.0.0.1:3111'}]))

    def test_missing_configuration_never_executes_old_auth(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'SETTINGS', Path(directory) / 'missing.json'), patch.dict(os.environ, {}, clear=True), patch.object(provider.os, 'execvpe') as execute:
            with self.assertRaises(OSError):
                provider.main()
            execute.assert_not_called()

    def test_launcher_consumes_json_env_without_forwarding_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'SETTINGS', Path(directory) / 'settings.json'), patch.dict(os.environ, {'CHEBY_PROVIDER_SETTINGS': json.dumps(settings())}, clear=True), patch.object(provider.os, 'execvpe') as execute:
            provider.main()
            env = execute.call_args.args[2]
            self.assertNotIn('CHEBY_PROVIDER_SETTINGS', env)
            self.assertNotIn('CHEBY_PROVIDER_SETTINGS', os.environ)
            self.assertNotIn('CHEBY_MODEL_API_KEY', env)
            self.assertEqual((Path(directory) / 'model-catalog.json').stat().st_mode & 0o777, 0o600)

    def test_glm_main_routes_codex_through_local_adapter(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(provider, 'SETTINGS', Path(directory) / 'settings.json'), \
                patch.dict(os.environ, {'CHEBY_PROVIDER_SETTINGS': json.dumps(settings(provider='glm'))}, clear=True), \
                patch.object(provider, 'start_chat_adapter', return_value=('http://127.0.0.1:34567', 'fixture-local-token')) as start, \
                patch.object(provider.os, 'execvpe') as execute:
            provider.main()
            start.assert_called_once_with('glm', 'https://api.z.ai/api/paas/v4', 'fixture-main-secret')
            command = execute.call_args.args[1]
            joined = ' '.join(command)
            self.assertIn('http://127.0.0.1:34567', joined)
            self.assertNotIn('https://api.z.ai/api/paas/v4', joined)
            env = execute.call_args.args[2]
            self.assertEqual(env['CHEBY_MODEL_API_KEY'], 'fixture-local-token')
            self.assertNotIn('fixture-main-secret', json.dumps(env))

    def test_minimax_main_routes_codex_through_local_adapter(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(provider, 'SETTINGS', Path(directory) / 'settings.json'), \
                patch.dict(os.environ, {'CHEBY_PROVIDER_SETTINGS': json.dumps(settings(provider='minimax'))}, clear=True), \
                patch.object(provider, 'start_chat_adapter', return_value=('http://127.0.0.1:34568', 'fixture-local-token')) as start, \
                patch.object(provider.os, 'execvpe') as execute:
            provider.main()
            start.assert_called_once_with('minimax', 'https://api.minimaxi.com/v1', 'fixture-main-secret')
            joined = ' '.join(execute.call_args.args[1])
            self.assertIn('http://127.0.0.1:34568', joined)
            self.assertNotIn('https://api.minimaxi.com/v1', joined)


class GlmAdapterTests(unittest.TestCase):
    def payload(self):
        return {
            'model': 'glm-5.3-flash',
            'instructions': 'Use the phone tools carefully.',
            'reasoning': {'effort': 'max'},
            'input': [{
                'type': 'message', 'role': 'user',
                'content': [
                    {'type': 'input_text', 'text': 'Inspect this screen.'},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,ZmFrZQ=='},
                ],
            }],
            'tools': [{
                'type': 'namespace', 'name': 'mcp__phonebridge',
                'tools': [{
                    'name': 'android_phone_status', 'description': 'Read phone status.',
                    'input_schema': {'type': 'object', 'properties': {}},
                }],
            }],
            'tool_choice': 'auto',
        }

    def test_native_image_and_namespace_translate_to_chat(self):
        request, aliases = glm_adapter.chat_request(self.payload())
        self.assertEqual(request['model'], 'glm-5.3-flash')
        self.assertEqual(request['reasoning_effort'], 'max')
        self.assertEqual(request['thinking'], {'type': 'enabled', 'clear_thinking': True})
        self.assertEqual(request['messages'][0]['role'], 'system')
        parts = request['messages'][1]['content']
        self.assertEqual(parts[1]['image_url']['url'], 'data:image/png;base64,ZmFrZQ==')
        name = request['tools'][0]['function']['name']
        self.assertEqual(aliases[name], {
            'kind': 'namespace', 'namespace': 'mcp__phonebridge',
            'name': 'android_phone_status',
        })

    def test_minimax_reasoning_effort_maps_to_supported_thinking_toggle(self):
        payload = self.payload()
        payload['model'] = 'MiniMax-M3'
        payload['reasoning'] = {'effort': 'high'}
        request, _ = glm_adapter.chat_request(payload, 'MiniMax-M3', 'minimax')
        self.assertEqual(request['model'], 'MiniMax-M3')
        self.assertTrue(request['reasoning_split'])
        self.assertEqual(request['thinking'], {'type': 'adaptive'})
        self.assertNotIn('reasoning_effort', request)
        payload['reasoning'] = {'effort': 'low'}
        request, _ = glm_adapter.chat_request(payload, 'MiniMax-M3', 'minimax')
        self.assertEqual(request['thinking'], {'type': 'disabled'})

    def test_minimax_replays_real_reasoning_on_tool_continuation(self):
        payload = self.payload()
        payload['model'] = 'MiniMax-M3'
        payload['input'].extend([
            {
                'type': 'function_call', 'call_id': 'call_minimax',
                'name': 'android_phone_status', 'namespace': 'mcp__phonebridge',
                'arguments': '{}',
            },
            {
                'type': 'function_call_output', 'call_id': 'call_minimax',
                'output': '{"phone_online":true}',
            },
        ])
        request, _ = glm_adapter.chat_request(
            payload, 'MiniMax-M3', 'minimax', {'call_minimax': 'private reasoning'},
        )
        assistant = request['messages'][-2]
        self.assertEqual(assistant['reasoning_content'], 'private reasoning')
        self.assertEqual(assistant['tool_calls'][0]['id'], 'call_minimax')

    def test_minimax_reasoning_cache_is_bounded_and_only_records_tool_turns(self):
        cache = OrderedDict()
        glm_adapter.remember_reasoning({
            'choices': [{'message': {
                'reasoning_content': 'private reasoning',
                'tool_calls': [{'id': 'call_minimax'}],
            }}],
        }, cache)
        self.assertEqual(cache, {'call_minimax': 'private reasoning'})
        glm_adapter.remember_reasoning({
            'choices': [{'message': {'reasoning_content': 'unused', 'tool_calls': []}}],
        }, cache)
        self.assertNotIn('unused', cache.values())
        for index in range(glm_adapter.MAX_REASONING_CACHE_ENTRIES + 2):
            glm_adapter.remember_reasoning({
                'choices': [{'message': {
                    'reasoning_content': f'r{index}',
                    'tool_calls': [{'id': f'call_{index}'}],
                }}],
            }, cache)
        self.assertLessEqual(len(cache), glm_adapter.MAX_REASONING_CACHE_ENTRIES)
        self.assertNotIn('call_minimax', cache)

    def test_tool_call_and_history_round_trip(self):
        request, aliases = glm_adapter.chat_request(self.payload())
        alias = request['tools'][0]['function']['name']
        chat = {
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_fixture', 'type': 'function',
                    'function': {'name': alias, 'arguments': '{}'},
                }],
            }}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14},
        }
        events = glm_adapter.responses_events(chat, aliases)
        done = next(event['item'] for event in events if event['type'] == 'response.output_item.done')
        self.assertEqual(done['type'], 'function_call')
        self.assertEqual(done['namespace'], 'mcp__phonebridge')
        self.assertEqual(done['name'], 'android_phone_status')
        self.assertEqual(done['call_id'], 'call_fixture')

        payload = self.payload()
        payload['input'].extend([
            done,
            {'type': 'function_call_output', 'call_id': 'call_fixture',
             'output': '{"phone_online":true}'},
        ])
        followup, _ = glm_adapter.chat_request(payload)
        self.assertEqual(followup['messages'][-2]['tool_calls'][0]['id'], 'call_fixture')
        self.assertEqual(followup['messages'][-1]['role'], 'tool')
        self.assertEqual(followup['messages'][-1]['tool_call_id'], 'call_fixture')

    def test_tool_image_output_remains_native_multimodal(self):
        request, aliases = glm_adapter.chat_request(self.payload())
        alias = request['tools'][0]['function']['name']
        events = glm_adapter.responses_events({
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_screenshot', 'type': 'function',
                    'function': {'name': alias, 'arguments': '{}'},
                }],
            }}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
        }, aliases)
        done = next(event['item'] for event in events
                    if event['type'] == 'response.output_item.done')
        payload = self.payload()
        payload['input'].extend([
            done,
            {'type': 'function_call_output', 'call_id': 'call_screenshot', 'output': [
                {'type': 'input_text', 'text': 'Android screenshot saved.'},
                {'type': 'input_image',
                 'image_url': 'data:image/png;base64,ZmFrZS1waXhlbHM='},
            ]},
        ])

        followup, _ = glm_adapter.chat_request(payload)
        self.assertEqual(followup['messages'][-3]['role'], 'assistant')
        self.assertEqual(followup['messages'][-2], {
            'role': 'tool', 'tool_call_id': 'call_screenshot',
            'content': 'Android screenshot saved.',
        })
        image_message = followup['messages'][-1]
        self.assertEqual(image_message['role'], 'user')
        self.assertEqual(image_message['content'][1]['type'], 'image_url')
        self.assertEqual(image_message['content'][1]['image_url']['url'],
                         'data:image/png;base64,ZmFrZS1waXhlbHM=')
        self.assertNotIn('ZmFrZS1waXhlbHM=', image_message['content'][0]['text'])

    def test_stringified_phonebridge_artifact_becomes_native_image(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(glm_adapter, 'PHONEBRIDGE_ARTIFACT_ROOT', Path(directory)):
            image = Path(directory) / 'phone-fixture.jpg'
            image.write_bytes(b'\xff\xd8\xfffixture-pixels')
            payload = self.payload()
            payload['input'].extend([
                {
                    'type': 'function_call', 'call_id': 'call_screenshot',
                    'name': 'android_phone_status', 'namespace': 'mcp__phonebridge',
                    'arguments': '{}',
                },
                {
                    'type': 'function_call_output', 'call_id': 'call_screenshot',
                    'output': json.dumps({'artifact_path': str(image)}),
                },
            ])
            followup, _ = glm_adapter.chat_request(payload)
        self.assertEqual(followup['messages'][-2]['role'], 'tool')
        self.assertEqual(followup['messages'][-1]['role'], 'user')
        url = followup['messages'][-1]['content'][1]['image_url']['url']
        self.assertEqual(url, 'data:image/jpeg;base64,/9j/Zml4dHVyZS1waXhlbHM=')

    def test_stringified_phonebridge_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside, \
                patch.object(glm_adapter, 'PHONEBRIDGE_ARTIFACT_ROOT', Path(directory)):
            invalid = Path(directory) / 'not-an-image.jpg'
            invalid.write_text('not an image')
            escaped = Path(directory) / 'escape.jpg'
            escaped.symlink_to(Path(outside) / 'outside.jpg')
            self.assertIsNone(glm_adapter.trusted_phonebridge_image(
                json.dumps({'artifact_path': str(invalid)})))
            self.assertIsNone(glm_adapter.trusted_phonebridge_image(
                json.dumps({'artifact_path': str(escaped)})))

    def test_message_response_and_fail_closed_shapes(self):
        events = glm_adapter.responses_events({
            'choices': [{'finish_reason': 'stop', 'message': {'content': '四'}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3},
        }, {})
        deltas = [event['delta'] for event in events if event['type'] == 'response.output_text.delta']
        self.assertEqual(deltas, ['四'])
        self.assertEqual(events[-1]['type'], 'response.completed')
        with self.assertRaises(ValueError):
            glm_adapter.chat_request({**self.payload(), 'model': 'other'})
        with self.assertRaises(ValueError):
            glm_adapter.chat_tools([{'type': 'computer'}])
        self.assertIsNone(glm_adapter.NoRedirect().redirect_request(None, None, 307, None, {}, 'https://other.example'))


class BridgeTests(unittest.TestCase):
    def test_cached_reconnect_replacement_tombstone_and_bad_file(self):
        script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function consumeProviderSettings()');
const end = source.indexOf('\nfunction validateUpgrade', start);
const context = {fs, Buffer, TextDecoder, PROVIDER_SETTINGS_PATH: process.argv[2], MAX_PROVIDER_SETTINGS_BYTES: 65536, cachedProviderSettings: null};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const write = value => fs.writeFileSync(process.argv[2], JSON.stringify(value), {mode: 0o600});
assert.throws(() => context.consumeProviderSettings());
write({provider:'glm',apiKey:'fixture-first'});
const first = context.consumeProviderSettings();
assert.equal(fs.existsSync(process.argv[2]), false);
assert.equal(context.consumeProviderSettings(), first);
write({provider:'minimax',apiKey:'fixture-second'});
assert.equal(JSON.parse(context.consumeProviderSettings()).apiKey, 'fixture-second');
write({provider:'minimax',apiKey:''});
assert.equal(JSON.parse(context.consumeProviderSettings()).apiKey, '');
fs.writeFileSync(process.argv[2], '{bad', {mode:0o600});
assert.throws(() => context.consumeProviderSettings());
assert.throws(() => context.consumeProviderSettings());
write({apiKey:'fixture-insecure'});
fs.chmodSync(process.argv[2],0o644);
assert.throws(() => context.consumeProviderSettings());
assert.equal(fs.existsSync(process.argv[2]),false);
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), '-e', script, str(REPO / 'tools/standalone/phone_codex_stdio_bridge.mjs'), str(Path(directory) / 'settings.json')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
