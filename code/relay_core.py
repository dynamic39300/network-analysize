#!/usr/bin/env python3
"""Headless NetCare entry point. Invoking a command does not grant network write authority."""
import argparse
from dataclasses import asdict
import json
import multiprocessing
import os
from pathlib import Path
import sys
import signal
import threading
import uuid

from relay.core import CoreRuntime
from relay.profiles import parse_import


def emit(value):
    # ASCII JSON is also UTF-8 and stays valid when Windows redirects to a legacy code page.
    print(json.dumps(value, ensure_ascii=True, default=str), flush=True)


def confirm_exact(token, warning):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError('Interactive confirmation requires a local terminal, not piped input')
    print(warning, file=sys.stderr, flush=True)
    return input('Type ' + token + ' to confirm: ').strip() == token


def confirm_command(runtime, run):
    digest = runtime.agent._proposal_hash(run)
    emit({'command_review': {'run_id': run.id, 'proposal_hash': digest,
                            'proposal': asdict(run.proposals[0]), 'contains_private_details': True}})
    if not confirm_exact('EXECUTE ' + digest,
            '此命令以当前用户启动，但不是沙箱；它可能访问用户文件、联网或调用已有权限辅助工具。请检查完整 argv/stdin、目标和恢复限制；不要确认不理解的命令。'):
        runtime.agent.cancel(run)
        return run, runtime._report(run)
    return runtime.execute_command(run, digest, acknowledge_unrestricted=True)


def interactive_investigation(runtime, run, report):
    while run.stage == 'awaiting_authorization':
        if runtime.agent._is_dynamic(run):
            run, report = confirm_command(runtime, run)
        else:
            digest = runtime.agent._proposal_hash(run)
            emit({'repair_review': {'run_id': run.id, 'proposal_hash': digest,
                                   'proposal': asdict(run.proposals[0]), 'contains_private_details': True}})
            if not confirm_exact('REPAIR ' + digest, '请检查本次成熟修复的具体对象、目标值和恢复范围；这次确认不授权后续方案。'):
                runtime.agent.cancel(run)
                return run, runtime._report(run)
            run, report = runtime.execute_proposal(run, digest)
        if run.stage == 'cancelled' or not runtime.agent.can_resume_investigation(run):
            break
        run, report = runtime.resume_investigation(run)
    return run, report


def main(argv=None, runtime_factory=CoreRuntime):
    parser = argparse.ArgumentParser(description='NetCare assurance core; dynamic jobs require separate interactive confirmation')
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='Run an explicit check and print a redacted report')
    check.add_argument('--raw', action='store_true', help='Print private identifiers and full local evidence explicitly')
    watch = commands.add_parser('watch', help='Enable read-only guard until Ctrl+C; no autostart installation')
    serve = commands.add_parser('serve', help='Run the authenticated local core; guard starts disabled')
    serve.add_argument('--launch-id', type=lambda value: uuid.UUID(hex=value).hex)
    serve.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    serve.add_argument('--restore-guard', action='store_true', help='Restore only an explicit core guard choice; never model or repair authority')
    commands.add_parser('desktop', help='Connect the native macOS desktop to an already running local core')
    investigate = commands.add_parser('investigate', help='Investigate with local tools and an optional model')
    investigate.add_argument('--raw', action='store_true', help='Print private evidence and model hypotheses explicitly')
    investigate.add_argument('--interactive', action='store_true', help='Confirm each exact proposal separately, then continue the same investigation')
    command = commands.add_parser('command', help='Prepare a private dynamic-command document; never auto-execute')
    command.add_argument('file', type=Path)
    command.add_argument('--interactive', action='store_true', help='Require exact proposal-hash confirmation in a local terminal')
    command.add_argument('--raw', action='store_true', help='Print private command output and evidence in the receipt')
    review = commands.add_parser('review-command', help='Inspect a dynamic receipt; acknowledge only after human review')
    review.add_argument('run_id')
    review.add_argument('--interactive', action='store_true', help='Explicitly record manual review, not verified recovery')
    for command in (watch, investigate, serve):
        command.add_argument('--model', help='Explicit Responses-compatible model name; no model by default')
        command.add_argument('--model-endpoint', default='https://api.openai.com/v1/responses')
        command.add_argument('--allow-model-upload', action='store_true',
            help='Consent to send minimized evidence to this model for this process; calls may incur charges')
    profiles = commands.add_parser('profiles').add_subparsers(dest='profile_command', required=True)
    profiles.add_parser('list')
    profiles.add_parser('import').add_argument('file', type=Path)
    profiles.add_parser('export').add_argument('id')
    profiles.add_parser('activate').add_argument('id')
    profiles.add_parser('remove').add_argument('id')
    args = parser.parse_args(argv)
    if getattr(args, 'model', None) and not args.allow_model_upload:
        parser.error('--model requires explicit --allow-model-upload consent')
    runtime = None
    service = None
    old_signals = {}
    try:
        if args.command == 'desktop':
            from relay.core import default_directory
            from relay.remote_desktop import run_desktop
            return run_desktop(args.data_dir or default_directory())
        options = {}
        if getattr(args, 'model', None):
            from relay.models import ModelConsent, ResponsesModel
            model = ResponsesModel(args.model, args.model_endpoint, os.environ.get('RELAY_MODEL_API_KEY', ''))
            options.update(model=model, model_consent=ModelConsent(model))
        runtime = runtime_factory(data_dir=args.data_dir, on_report=emit, **options)
        if args.command == 'serve':
            from relay.core_service import CoreService
            stopped = threading.Event()
            service = CoreService(runtime, stop_requested=stopped, launch_id=args.launch_id, managed=args.managed)
            for kind in (signal.SIGTERM, signal.SIGINT):
                old_signals[kind] = signal.signal(kind, lambda *_: stopped.set())
            if args.restore_guard:
                runtime.restore_guard()
            service.start()
            emit({'schema': 'relay-local-v1', 'core_instance': service.instance, 'state': 'ready',
                  'guard_enabled': runtime.guard.schedule.state()['enabled'], 'network_writes': 0})
            stopped.wait()
            return 0
        if args.command in ('check', 'investigate'):
            run, report = runtime.check() if args.command == 'check' else runtime.investigate()
            if getattr(args, 'interactive', False):
                run, report = interactive_investigation(runtime, run, report)
            emit(runtime.raw_report(run) if args.raw else report)
            complete = (run.health == 'healthy' and not report['recovery_pending']
                        and run.stage in ('observed', 'finished')
                        and all(state == 'ok' for state in report['persistence'].values()))
            return 0 if complete else 2
        if args.command == 'command':
            from relay.models import strict_json
            with args.file.open('rb') as stream:
                content = stream.read(65537)
            if len(content) > 65536:
                raise ValueError('Command document exceeds 64 KiB')
            run = runtime.prepare_command(strict_json(content))
            if not args.interactive:
                emit({'command_review': {'run_id': run.id, 'proposal_hash': runtime.agent._proposal_hash(run),
                      'proposal': asdict(run.proposals[0]), 'contains_private_details': True, 'executed': False}})
                return 2
            run, report = confirm_command(runtime, run)
            emit(runtime.raw_report(run) if args.raw else report)
            return 2
        if args.command == 'review-command':
            from relay.dynamic import digest
            record = runtime.command_record(args.run_id)
            receipt = record.get('receipt') or {}
            if receipt.get('kind') != 'dynamic_command':
                raise ValueError('Not a dynamic command receipt')
            receipt_hash = digest(receipt)
            emit({'receipt_review': {'run_id': args.run_id, 'receipt_hash': receipt_hash,
                  'receipt': receipt, 'contains_private_details': True, 'effects_verified': False}})
            if args.interactive and confirm_exact('REVIEW ' + receipt_hash,
                    '仅在你已核对命令影响和恢复资料后确认。该操作解除此任务阻止，不表示 Agent 验证了全部副作用。'):
                note = input('Manual review note: ')
                emit(runtime.review_command(args.run_id, receipt_hash, note))
                return 0
            return 2
        if args.command == 'watch':
            emit(runtime.start_guard(persist=False))
            threading.Event().wait()
        elif args.profile_command == 'list':
            emit({'active': runtime.profiles.binding(), 'profiles': [
                {key: profile[key] for key in ('id', 'name', 'revision', 'source')} for profile in runtime.profiles.profiles]})
        elif args.profile_command == 'import':
            with args.file.open('rb') as stream:
                content = stream.read(256 * 1024 + 1)
            if len(content) > 256 * 1024:
                raise ValueError('Profile file exceeds 256 KiB')
            profile = runtime.profiles.save(parse_import(content.decode('utf-8')), source='user_import')
            emit({'id': profile['id'], 'revision': profile['revision'], 'network_writes': 0})
        elif args.profile_command == 'export':
            emit(runtime.profiles.document(args.id))
        elif args.profile_command == 'activate':
            runtime.profiles.activate(args.id)
            emit(runtime.profiles.binding())
        elif args.profile_command == 'remove':
            runtime.profiles.remove(args.id)
            emit({'removed': args.id})
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Raw system exceptions can contain local paths, hostnames or policy text.
        emit({'error': type(exc).__name__, 'message': '操作未完成；请检查任务记录、实际状态、本地存储或输入。未自动重放命令。'})
        return 1
    finally:
        if service:
            service.close()
        elif runtime:
            runtime.close()
        for kind, handler in old_signals.items():
            signal.signal(kind, handler)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
