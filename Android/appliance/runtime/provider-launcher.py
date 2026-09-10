#!/usr/bin/env python3
"""Translate appliance settings to upstream Codex configuration; no agent logic."""
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import stat
from urllib.parse import urlsplit

SETTINGS = Path('/root/.cheby/provider-settings.json')
PROVIDERS = {
    'glm': {
        'model': 'glm-5.3-flash', 'url': 'https://api.z.ai/api/paas/v4',
        'default_effort': 'low', 'efforts': ('low', 'high', 'max'), 'chat_adapter': True,
    },
    'openai': {
        'model': 'gpt-5.6-sol', 'url': 'https://api.openai.com/v1',
        'default_effort': 'high',
        'efforts': ('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'chat_adapter': False,
    },
}
MAX_SETTINGS = 65536
BASE_INSTRUCTIONS = Path(__file__).with_name('codex-base-instructions.md')
CHAT_ADAPTER = Path(__file__).with_name('glm-chat-adapter.py')
BASE_INSTRUCTIONS_SHA256 = '294602e3502bb0ff840a82cd1d08ac526125a6cdf763e1f883e8d74f47cc2a8d'
RESERVED_MCP_NAMES = {'phonebridge', 'cheby_vision', 'vision', 'codex', 'local_mcp'}


def boolean(value):
    if type(value) is not bool:
        raise ValueError('Expected a boolean setting')
    return value


def credential(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('A valid API credential is required')
    return value


def optional_text(settings, name, default):
    value = settings.get(name, '')
    if not isinstance(value, str):
        raise ValueError('Invalid text setting')
    return value or default


def endpoint(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError('Invalid endpoint')
    url = urlsplit(value)
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid endpoint port')
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invalid endpoint')
    if url.scheme != 'https' and not (
        url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
    ):
        raise ValueError('Endpoint requires HTTPS or local loopback')
    return value.rstrip('/')


def launch_config(settings, catalog_path, transport_base_url=None, transport_key=None):
    if not isinstance(settings, dict):
        raise ValueError('Expected provider settings')
    if any(name in settings for name in ('visionEnabled', 'visionBaseUrl', 'visionKey', 'clearVisionKey')):
        raise ValueError('Independent vision MCP is not supported')
    provider = settings.get('provider')
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError('Choose an available multimodal model')
    spec = PROVIDERS[provider]
    model = spec['model']
    effort = optional_text(settings, 'reasoningEffort', spec['default_effort'])
    if effort not in spec['efforts']:
        raise ValueError('Unsupported reasoning effort')
    configured_url = endpoint(optional_text(settings, 'baseUrl', spec['url']))
    if provider == 'openai' and configured_url != spec['url']:
        raise ValueError('OpenAI subscription login uses the official service')
    if transport_base_url is not None:
        transport_base_url = endpoint(transport_base_url)
    if transport_key is not None:
        transport_key = credential(transport_key)
    env = {}
    config = {
        'model': model,
        'model_reasoning_effort': effort,
        'model_reasoning_summary': 'none',
        # The phone uses its explicit local MCPs, not account-wide Apps connectors.
        'features.apps': False,
        # Prevent ordinary model shell tools from inheriting provider credentials.
        # This is hygiene, not a security boundary against same-UID /proc access.
        'shell_environment_policy.exclude': ['CHEBY_MODEL_API_KEY', 'CHEBY_MCP_TOKEN_*', 'CHEBY_PROVIDER_SETTINGS'],
    }
    if provider != 'openai':
        key = credential(settings.get('apiKey'))
        env['CHEBY_MODEL_API_KEY'] = transport_key or key
        config.update({
            'model_provider': 'cheby_user',
            'model_catalog_json': str(catalog_path),
            'model_providers.cheby_user.name': provider,
            'model_providers.cheby_user.base_url': transport_base_url or configured_url,
            'model_providers.cheby_user.env_key': 'CHEBY_MODEL_API_KEY',
            'model_providers.cheby_user.wire_api': 'responses',
            'model_providers.cheby_user.requires_openai_auth': False,
            'model_providers.cheby_user.supports_websockets': False,
        })
    # Declares capabilities, not scene-specific instructions. GLM-5.3-Flash
    # receives phone screenshots as native image input.
    original_instructions = BASE_INSTRUCTIONS.read_bytes()
    if hashlib.sha256(original_instructions).hexdigest() != BASE_INSTRUCTIONS_SHA256:
        raise ValueError('Pinned upstream instructions are missing or changed')
    mobile_instructions = BASE_INSTRUCTIONS.with_name('mobile-experience-instructions.md')
    if mobile_instructions.is_file():
        # Applies to OpenAI native catalog and configured providers alike; keep upstream base pinned.
        config['developer_instructions'] = mobile_instructions.read_text(encoding='utf-8')
    catalog = {'models': [{
        'slug': model, 'display_name': model, 'description': provider,
        'default_reasoning_level': effort,
        'supported_reasoning_levels': [{'effort': level, 'description': level.capitalize() + ' reasoning'} for level in spec['efforts']],
        'shell_type': 'shell_command', 'visibility': 'list', 'supported_in_api': True,
        'priority': 0, 'base_instructions': original_instructions.decode('utf-8'),
        'supports_reasoning_summaries': True, 'default_reasoning_summary': 'none',
        'support_verbosity': False, 'truncation_policy': {'mode': 'bytes', 'limit': 10000},
        'supports_parallel_tool_calls': True, 'experimental_supported_tools': [],
        'input_modalities': ['text', 'image'],
        'apply_patch_tool_type': 'freeform',
        'prefer_websockets': False, 'context_window': 1048576,
        'max_context_window': 1048576, 'effective_context_window_percent': 95,
    }]}
    servers = settings.get('mcpServers', [])
    if not isinstance(servers, list) or len(servers) > 12:
        raise ValueError('Invalid MCP services')
    names = set()
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            raise ValueError('Invalid MCP service')
        # Names cannot override phonebridge or built-in services.
        name = server.get('name')
        if not isinstance(name, str) or not name or len(name) > 40 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in name):
            raise ValueError('Invalid MCP name')
        if name.lower() in names or name.lower() in RESERVED_MCP_NAMES or name.lower().startswith(('cheby_', 'user_')):
            raise ValueError('Duplicate or reserved MCP name')
        names.add(name.lower())
        authorized = boolean(server.get('remoteAuthorized', False))
        enabled = boolean(server.get('enabled', True))
        if not enabled:
            continue
        prefix = 'mcp_servers.user_' + name
        config[prefix + '.url'] = endpoint(server['url'])
        if urlsplit(config[prefix + '.url']).hostname not in ('localhost', '127.0.0.1', '::1') and not authorized:
            raise ValueError('Remote MCP requires explicit service authorization')
        if server.get('token'):
            variable = f'CHEBY_MCP_TOKEN_{index}'
            env[variable] = credential(server['token'])
            config[prefix + '.bearer_token_env_var'] = variable
    args = []
    for name, value in config.items():
        args.extend(['-c', name + '=' + json.dumps(value, ensure_ascii=False)])
    return args, env, catalog


def start_chat_adapter(provider, upstream_base_url, upstream_key):
    if provider not in PROVIDERS or not PROVIDERS[provider]['chat_adapter']:
        raise ValueError('Provider does not use the chat adapter')
    if not CHAT_ADAPTER.is_file():
        raise ValueError('Pinned chat adapter is missing')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    os.set_inheritable(listener.fileno(), True)
    local_token = secrets.token_urlsafe(48)
    adapter_env = {
        'PATH': '/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'CHEBY_ADAPTER_TOKEN': local_token,
        'CHEBY_UPSTREAM_API_KEY': upstream_key,
        'CHEBY_UPSTREAM_BASE_URL': endpoint(upstream_base_url),
        'CHEBY_UPSTREAM_PROVIDER': provider,
        'CHEBY_UPSTREAM_MODEL': PROVIDERS[provider]['model'],
        'CHEBY_LISTEN_FD': str(listener.fileno()),
        'CHEBY_PARENT_PID': str(os.getpid()),
    }
    try:
        process = subprocess.Popen(
            ['/usr/bin/python3', '-I', str(CHAT_ADAPTER), '--serve'],
            env=adapter_env, pass_fds=(listener.fileno(),), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if process.poll() is not None:
            raise ValueError('Chat adapter could not start')
        return f'http://127.0.0.1:{listener.getsockname()[1]}', local_token
    finally:
        listener.close()


def main():
    env = os.environ.copy()
    raw = env.pop('CHEBY_PROVIDER_SETTINGS', None)
    os.environ.pop('CHEBY_PROVIDER_SETTINGS', None)
    if raw is None:
        # Direct CLI fallback. Android/bridge normally supplies a one-shot env.
        with os.fdopen(os.open(SETTINGS, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8') as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError('Private settings required')
            raw = source.read(MAX_SETTINGS + 1)
        SETTINGS.unlink()
    if len(raw.encode('utf-8')) > MAX_SETTINGS:
        raise ValueError('Settings exceed limit')
    settings = json.loads(raw)
    catalog_path = SETTINGS.with_name('model-catalog.json')
    transport_base_url = None
    transport_key = None
    provider = settings.get('provider') if isinstance(settings, dict) else None
    if provider in PROVIDERS and PROVIDERS[provider]['chat_adapter']:
        upstream_base_url = endpoint(optional_text(settings, 'baseUrl', PROVIDERS[provider]['url']))
        upstream_key = credential(settings.get('apiKey'))
        transport_base_url, transport_key = start_chat_adapter(provider, upstream_base_url, upstream_key)
    args, extra_env, catalog = launch_config(
        settings, catalog_path, transport_base_url=transport_base_url,
        transport_key=transport_key,
    )
    env.update(extra_env)
    fd, temporary = tempfile.mkstemp(prefix='model-catalog-', dir=catalog_path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(catalog, handle)
        os.replace(temporary, catalog_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    os.execvpe('codex', ['codex', *args, *sys.argv[1:]], env)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError):
        print('Model configuration could not be loaded. Check phone settings.', file=sys.stderr)
        sys.exit(1)
