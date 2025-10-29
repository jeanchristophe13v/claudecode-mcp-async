#!/usr/bin/env python3
"""
Claude Code MCP Server Wrapper with Async Support
将 Claude Code CLI 封装为符合 MCP 协议的 server，支持异步任务
"""
import sys
import json
import subprocess
import uuid
import os
import time
import logging
import signal
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# 配置日志系统
logging.basicConfig(
    filename='/tmp/claude_code_mcp_debug.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.info("=== Claude Code MCP Server Starting ===")

# 任务存储目录
TASK_DIR = Path("/tmp/claude_code_tasks")
TASK_DIR.mkdir(exist_ok=True)

# ============================================
# 僵尸进程自动回收机制
# ============================================
def sigchld_handler(signum, frame):
    """
    SIGCHLD 信号处理器：自动回收已终止的子进程，防止僵尸进程累积

    当子进程终止时，内核会向父进程发送 SIGCHLD 信号。
    这个处理器会被自动调用，通过 os.waitpid() 回收所有已终止的子进程。
    """
    while True:
        try:
            # os.waitpid(-1, os.WNOHANG):
            #   -1: 等待任意子进程
            #   os.WNOHANG: 非阻塞模式，如果没有已终止的子进程立即返回 (0, 0)
            pid, status = os.waitpid(-1, os.WNOHANG)

            if pid == 0:
                # 没有更多已终止的子进程
                break

            # 记录回收信息
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
            logging.debug(f"Reaped child process PID {pid}, exit_code={exit_code}, status={status}")

        except ChildProcessError:
            # 没有子进程了
            break
        except Exception as e:
            # 处理其他异常（不应该发生，但保险起见）
            logging.warning(f"Error in SIGCHLD handler: {e}")
            break

# 注册 SIGCHLD 信号处理器
# 注意：在某些系统上，默认行为是 SIG_IGN（忽略），这会导致子进程自动回收
# 但显式设置处理器可以让我们记录日志，更好地调试
signal.signal(signal.SIGCHLD, sigchld_handler)
logging.info("SIGCHLD handler registered for automatic zombie process reaping")

def safe_read_file(file_path: Path) -> str:
    """安全地读取文件，处理各种编码和异常问题"""
    if not file_path.exists():
        logging.debug(f"File {file_path} does not exist")
        return ""

    try:
        # 优先尝试严格的 UTF-8 读取
        content = file_path.read_text(encoding='utf-8')
        logging.debug(f"Successfully read {file_path} with UTF-8 encoding ({len(content)} chars)")
        return content
    except UnicodeDecodeError as e:
        # 降级为 replace 模式
        logging.warning(f"UTF-8 decode error for {file_path}: {e}, using replace mode")
        try:
            content = file_path.read_text(encoding='utf-8', errors='replace')
            logging.debug(f"Read {file_path} with replace mode ({len(content)} chars)")
            return content
        except Exception as e2:
            logging.error(f"Failed to read {file_path} even with replace mode: {e2}")
            return f"[Error reading file: {e2}]"
    except PermissionError as e:
        logging.error(f"Permission denied reading {file_path}: {e}")
        return f"[Permission denied: {e}]"
    except Exception as e:
        logging.error(f"Unexpected error reading {file_path}: {e}")
        return f"[Unexpected error: {e}]"

def is_process_alive(pid: int) -> bool:
    """检测进程是否还在运行（排除僵尸进程）"""
    if pid is None:
        return False

    try:
        # 发送信号 0 检测进程是否存在
        os.kill(pid, 0)

        # 进程存在，但需要检查是否为僵尸进程
        try:
            # 使用 ps 命令检查进程状态
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'stat='],
                stdin=subprocess.DEVNULL,  # 避免从父进程 stdin 读取
                capture_output=True,
                text=True,
                timeout=1
            )
            if result.returncode == 0:
                stat = result.stdout.strip()
                # 如果状态以 'Z' 开头，说明是僵尸进程
                if stat.startswith('Z'):
                    logging.debug(f"PID {pid} is a zombie process")
                    return False
                return True
            else:
                # ps 命令失败，进程可能已经不存在
                return False
        except Exception as e:
            logging.warning(f"Failed to check process state for PID {pid}: {e}")
            # 如果无法检查状态，保守地认为进程还活着
            return True

    except ProcessLookupError:
        # 进程不存在
        return False
    except PermissionError:
        # 进程存在但没有权限，假设它还活着
        return True
    except Exception as e:
        logging.warning(f"Unexpected error checking PID {pid}: {e}")
        return False

def send_response(response: Dict[str, Any]) -> None:
    """发送JSON-RPC响应到stdout"""
    try:
        # 添加 ensure_ascii=False 支持中文
        output = json.dumps(response, ensure_ascii=False)
        print(output, flush=True)
        logging.debug(f"Sent response: id={response.get('id')}, size={len(output)} bytes")
    except (TypeError, ValueError) as e:
        # 序列化失败，返回错误响应
        logging.error(f"Failed to serialize response: {e}, response={response}")
        error_response = {
            'jsonrpc': '2.0',
            'id': response.get('id'),
            'error': {
                'code': -32603,
                'message': f'Response serialization failed: {str(e)}'
            }
        }
        print(json.dumps(error_response), flush=True)

def extract_result_from_claude_output(stdout: str, stderr: str, output_format: str = "text") -> str:
    """从 Claude Code 输出中提取结果"""
    logging.debug(f"Extracting result: stdout_len={len(stdout)}, stderr_len={len(stderr)}, format={output_format}")

    # 处理 stream-json 格式（修复输出丢失问题的关键）
    if output_format == "stream-json":
        try:
            lines = [l for l in stdout.strip().split('\n') if l.strip()]
            logging.debug(f"stream-json: found {len(lines)} JSONL lines")

            # 查找 type="result" 的对象
            for line in lines:
                try:
                    obj = json.loads(line)
                    if obj.get('type') == 'result':
                        result = obj.get('result', '')
                        logging.info(f"Extracted stream-json result: {len(result)} chars")
                        return result
                except json.JSONDecodeError:
                    continue

            # 如果没找到 result 对象，返回所有内容
            logging.warning("No 'result' object found in stream-json, returning raw output")
            return stdout.strip()
        except Exception as e:
            logging.warning(f"stream-json parse failed: {e}, falling back to text mode")
            return stdout.strip()

    if output_format == "json":
        try:
            data = json.loads(stdout.strip())
            if isinstance(data, dict):
                result = json.dumps(data, indent=2, ensure_ascii=False)
                logging.debug(f"Extracted JSON result: {len(result)} chars")
                return result
            return stdout.strip()
        except json.JSONDecodeError as e:
            logging.warning(f"JSON decode failed: {e}, falling back to text mode")

    result = stdout.strip()

    # 检查 strip 是否导致内容丢失
    if len(stdout) > 0 and len(result) == 0:
        logging.warning(f"stdout.strip() returned empty! Original length={len(stdout)}, repr={repr(stdout[:100])}")

    if not result and stderr:
        result = stderr.strip()
        logging.debug(f"stdout is empty, using stderr: {len(result)} chars")

    if not result:
        logging.warning("No output from Claude Code (both stdout and stderr empty)")
        return "No output from Claude Code"

    logging.debug(f"Extracted text result: {len(result)} chars")
    return result

def start_claude_async(
    prompt: str,
    working_dir: str = None,
    model: str = None,
    output_format: str = "stream-json",  # 修改默认值为 stream-json（解决输出丢失）
    skip_permissions: bool = True,
    additional_args: List[str] = None
) -> str:
    """启动异步 Claude Code 任务"""
    additional_args = additional_args or []
    task_id = str(uuid.uuid4())[:8]
    task_path = TASK_DIR / task_id

    logging.info(f"Starting async task {task_id}: prompt={prompt[:50]}...")

    # 构建命令
    cmd = ['claude', '--print', prompt]

    # 设置输出格式（修复：stream-json 需要配合 --verbose）
    if output_format and output_format != "text":
        cmd.extend(['--output-format', output_format])
        # stream-json 格式必须配合 --verbose 才能工作
        if output_format == "stream-json":
            cmd.append('--verbose')
            logging.debug("Using stream-json format with --verbose flag")

    if model:
        cmd.extend(['--model', model])

    if skip_permissions:
        cmd.append('--dangerously-skip-permissions')

    cmd.extend(additional_args)

    # 创建任务文件
    stdout_file = task_path.with_suffix('.stdout')
    stderr_file = task_path.with_suffix('.stderr')

    cwd = working_dir or os.getcwd()

    # 启动后台进程
    try:
        with open(stdout_file, 'w') as stdout_f, open(stderr_file, 'w') as stderr_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,  # 重定向 stdin 到 /dev/null，避免子进程竞争读取父进程的 stdin
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                cwd=cwd,
                start_new_session=True
            )

        logging.info(f"Task {task_id} started with PID {proc.pid}")

        # 保存任务元数据（包含 PID）
        metadata = {
            'task_id': task_id,
            'pid': proc.pid,
            'status': 'running',
            'command': ' '.join(cmd),
            'working_dir': cwd,
            'output_format': output_format,
            'started_at': time.time()
        }

        with open(task_path.with_suffix('.meta'), 'w') as f:
            json.dump(metadata, f, indent=2)

        logging.debug(f"Task {task_id} metadata saved")

        return task_id

    except Exception as e:
        logging.error(f"Failed to start async task {task_id}: {e}\n{traceback.format_exc()}")
        raise

def check_task_status(task_id: str) -> Dict[str, Any]:
    """检查任务状态并返回结果"""
    logging.debug(f"Checking status for task {task_id}")
    task_path = TASK_DIR / task_id
    meta_file = task_path.with_suffix('.meta')

    if not meta_file.exists():
        logging.warning(f"Task {task_id} not found")
        return {
            'status': 'not_found',
            'error': f'Task {task_id} not found'
        }

    # 读取元数据（带异常处理）
    try:
        with open(meta_file, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        logging.debug(f"Task {task_id} metadata loaded: PID={metadata.get('pid')}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.error(f"Invalid metadata for task {task_id}: {e}")
        return {
            'status': 'error',
            'error': f'Invalid metadata: {str(e)}'
        }
    except Exception as e:
        logging.error(f"Failed to read metadata for task {task_id}: {e}")
        return {
            'status': 'error',
            'error': f'Failed to read metadata: {str(e)}'
        }

    output_format = metadata.get('output_format', 'text')
    pid = metadata.get('pid')

    # 检查进程是否还在运行（混合检测）
    stdout_file = task_path.with_suffix('.stdout')
    stderr_file = task_path.with_suffix('.stderr')

    # 1. 优先检测 PID 是否存活
    process_alive = is_process_alive(pid)

    # 2. 检查文件修改时间
    is_running = False
    if stdout_file.exists() or stderr_file.exists():
        latest_mtime = max(
            stdout_file.stat().st_mtime if stdout_file.exists() else 0,
            stderr_file.stat().st_mtime if stderr_file.exists() else 0
        )
        idle_time = time.time() - latest_mtime
        logging.debug(f"Task {task_id} idle time: {idle_time:.1f}s, process_alive={process_alive}")

        # 混合判断：进程存活 AND 最近有输出（或刚启动）
        if process_alive:
            if idle_time < 10:
                is_running = True
            else:
                # 进程存活但长时间无输出，仍认为在运行（可能是长耗时任务）
                is_running = True
        else:
            # 进程已退出
            is_running = False
    else:
        # 文件不存在，但进程可能刚启动
        is_running = process_alive

    if is_running:
        elapsed = time.time() - metadata['started_at']
        logging.debug(f"Task {task_id} is still running, elapsed={elapsed:.1f}s")
        return {
            'status': 'running',
            'task_id': task_id,
            'elapsed_seconds': int(elapsed),
            'command': metadata['command'],
            'working_dir': metadata.get('working_dir', 'N/A')
        }
    else:
        # 任务已完成，读取结果（使用安全读取）
        logging.debug(f"Task {task_id} completed, reading output files")

        try:
            stdout = safe_read_file(stdout_file)
            stderr = safe_read_file(stderr_file)

            result = extract_result_from_claude_output(stdout, stderr, output_format)

            # 更新元数据
            metadata['status'] = 'completed'
            metadata['completed_at'] = time.time()
            try:
                with open(meta_file, 'w') as f:
                    json.dump(metadata, f, indent=2)
            except Exception as e:
                logging.warning(f"Failed to update metadata for task {task_id}: {e}")

            logging.info(f"Task {task_id} completed successfully, result length={len(result)}")

            return {
                'status': 'completed',
                'task_id': task_id,
                'result': result,
                'elapsed_seconds': int(metadata.get('completed_at', time.time()) - metadata['started_at']),
                'working_dir': metadata.get('working_dir', 'N/A')
            }
        except Exception as e:
            logging.error(f"Failed to read output for task {task_id}: {e}\n{traceback.format_exc()}")
            return {
                'status': 'error',
                'task_id': task_id,
                'error': f'Failed to read output: {str(e)}'
            }

def call_claude_sync(
    prompt: str,
    working_dir: str = None,
    model: str = None,
    output_format: str = "stream-json",  # 修改默认值（解决输出丢失）
    skip_permissions: bool = True,
    timeout: int = None,
    additional_args: List[str] = None
) -> str:
    """同步调用 Claude Code"""
    additional_args = additional_args or []
    cmd = ['claude', '--print', prompt]

    # 设置输出格式（修复：stream-json 需要配合 --verbose）
    if output_format and output_format != "text":
        cmd.extend(['--output-format', output_format])
        # stream-json 格式必须配合 --verbose 才能工作
        if output_format == "stream-json":
            cmd.append('--verbose')
            logging.debug("Using stream-json format with --verbose flag")

    if model:
        cmd.extend(['--model', model])

    if skip_permissions:
        cmd.append('--dangerously-skip-permissions')

    cmd.extend(additional_args)
    
    cwd = working_dir or os.getcwd()

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # 避免子进程从父进程的 stdin 读取
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd
        )
        return extract_result_from_claude_output(result.stdout, result.stderr, output_format)
    except subprocess.TimeoutExpired:
        return "Error: Claude Code execution timed out"
    except Exception as e:
        return f"Error calling Claude Code: {str(e)}"

def handle_request(request: Dict[str, Any]) -> None:
    """处理MCP请求"""
    method = request.get('method')
    request_id = request.get('id')
    params = request.get('params', {})
    
    if method == 'initialize':
        send_response({
            'jsonrpc': '2.0',
            'id': request_id,
            'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {
                    'name': 'claude-code-mcp',
                    'version': '0.1.0'
                }
            }
        })
    
    elif method == 'notifications/initialized':
        # 这是一个通知，不需要响应（id 为 None）
        logging.debug("Received initialized notification, no response needed")
        # 通知不需要发送响应，直接返回
        return
    
    elif method == 'tools/list':
        send_response({
            'jsonrpc': '2.0',
            'id': request_id,
            'result': {
                'tools': [
                    {
                        'name': 'claude_code_execute',
                        'description': 'Execute Claude Code synchronously with full control over parameters. Returns the result after completion. Common usage: prompt="your task", working_dir="/path/to/project"',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {
                                'prompt': {
                                    'type': 'string',
                                    'description': 'The task prompt for Claude Code to execute'
                                },
                                'working_dir': {
                                    'type': 'string',
                                    'description': 'Working directory for the task (default: current directory)'
                                },
                                'model': {
                                    'type': 'string',
                                    'description': 'Model to use (e.g., "sonnet", "opus", "haiku")',
                                    'enum': ['sonnet', 'opus', 'haiku']
                                },
                                'output_format': {
                                    'type': 'string',
                                    'description': 'Output format: "text" (default), "json", or "stream-json"',
                                    'enum': ['text', 'json', 'stream-json'],
                                    'default': 'text'
                                },
                                'skip_permissions': {
                                    'type': 'boolean',
                                    'description': 'Skip permission checks for automation (default: true)',
                                    'default': True
                                },
                                'timeout': {
                                    'type': 'integer',
                                    'description': 'Timeout in seconds (default: no limit)'
                                },
                                'additional_args': {
                                    'type': 'array',
                                    'items': {'type': 'string'},
                                    'description': 'Additional CLI arguments to pass to Claude Code'
                                }
                            },
                            'required': ['prompt']
                        }
                    },
                    {
                        'name': 'claude_code_execute_async',
                        'description': 'Start a Claude Code task in the background and return immediately with a task_id. Use claude_code_check_result to retrieve the result later. This allows you to continue working while Claude Code runs.',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {
                                'prompt': {
                                    'type': 'string',
                                    'description': 'The task prompt for Claude Code to execute'
                                },
                                'working_dir': {
                                    'type': 'string',
                                    'description': 'Working directory for the task (default: current directory)'
                                },
                                'model': {
                                    'type': 'string',
                                    'description': 'Model to use (e.g., "sonnet", "opus", "haiku")',
                                    'enum': ['sonnet', 'opus', 'haiku']
                                },
                                'output_format': {
                                    'type': 'string',
                                    'description': 'Output format: "text" (default), "json", or "stream-json"',
                                    'enum': ['text', 'json', 'stream-json'],
                                    'default': 'text'
                                },
                                'skip_permissions': {
                                    'type': 'boolean',
                                    'description': 'Skip permission checks for automation (default: true)',
                                    'default': True
                                },
                                'additional_args': {
                                    'type': 'array',
                                    'items': {'type': 'string'},
                                    'description': 'Additional CLI arguments to pass to Claude Code'
                                }
                            },
                            'required': ['prompt']
                        }
                    },
                    {
                        'name': 'claude_code_check_result',
                        'description': 'Check the status of an async Claude Code task. Returns running/completed status and the result if available.',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {
                                'task_id': {
                                    'type': 'string',
                                    'description': 'The task_id returned by claude_code_execute_async'
                                }
                            },
                            'required': ['task_id']
                        }
                    }
                ]
            }
        })
    
    elif method == 'tools/call':
        tool_name = params.get('name')
        arguments = params.get('arguments', {})
        
        if tool_name == 'claude_code_execute':
            prompt = arguments.get('prompt')
            working_dir = arguments.get('working_dir')
            model = arguments.get('model')
            output_format = arguments.get('output_format', 'text')
            skip_permissions = arguments.get('skip_permissions', True)
            timeout = arguments.get('timeout')
            additional_args = arguments.get('additional_args', [])
            
            result = call_claude_sync(
                prompt=prompt,
                working_dir=working_dir,
                model=model,
                output_format=output_format,
                skip_permissions=skip_permissions,
                timeout=timeout,
                additional_args=additional_args
            )
            
            send_response({
                'jsonrpc': '2.0',
                'id': request_id,
                'result': {
                    'content': [{'type': 'text', 'text': result}]
                }
            })
        
        elif tool_name == 'claude_code_execute_async':
            prompt = arguments.get('prompt')
            working_dir = arguments.get('working_dir')
            model = arguments.get('model')
            output_format = arguments.get('output_format', 'text')
            skip_permissions = arguments.get('skip_permissions', True)
            additional_args = arguments.get('additional_args', [])
            
            task_id = start_claude_async(
                prompt=prompt,
                working_dir=working_dir,
                model=model,
                output_format=output_format,
                skip_permissions=skip_permissions,
                additional_args=additional_args
            )
            
            send_response({
                'jsonrpc': '2.0',
                'id': request_id,
                'result': {
                    'content': [{
                        'type': 'text',
                        'text': f'Claude Code task started in background.\nTask ID: {task_id}\n\nUse claude_code_check_result(task_id="{task_id}") to retrieve the result.'
                    }]
                }
            })
        
        elif tool_name == 'claude_code_check_result':
            task_id = arguments.get('task_id')
            
            if not task_id:
                send_response({
                    'jsonrpc': '2.0',
                    'id': request_id,
                    'error': {
                        'code': -32602,
                        'message': 'task_id is required'
                    }
                })
                return
            
            status_info = check_task_status(task_id)
            
            if status_info['status'] == 'running':
                text = f"Task {task_id} is still running.\nElapsed: {status_info['elapsed_seconds']}s\nWorking Directory: {status_info['working_dir']}\nCommand: {status_info['command']}"
            elif status_info['status'] == 'completed':
                text = f"Task {task_id} completed in {status_info['elapsed_seconds']}s.\nWorking Directory: {status_info['working_dir']}\n\nResult:\n{status_info['result']}"
            else:
                text = status_info.get('error', 'Unknown error')
            
            send_response({
                'jsonrpc': '2.0',
                'id': request_id,
                'result': {
                    'content': [{'type': 'text', 'text': text}]
                }
            })
        
        else:
            send_response({
                'jsonrpc': '2.0',
                'id': request_id,
                'error': {
                    'code': -32601,
                    'message': f'Unknown tool: {tool_name}'
                }
            })
    
    else:
        send_response({
            'jsonrpc': '2.0',
            'id': request_id,
            'error': {
                'code': -32601,
                'message': f'Method not found: {method}'
            }
        })

def main():
    """主循环：从stdin读取请求，处理后写入stdout"""
    logging.info("Main loop starting")
    logging.debug(f"stdin closed={sys.stdin.closed}, stdout closed={sys.stdout.closed}")
    request_count = 0
    try:
        for line in sys.stdin:
            request_count += 1
            logging.debug(f"Read line #{request_count}, length={len(line)}")
            line = line.strip()
            if not line:
                logging.debug(f"Empty line, skipping")
                continue

            try:
                request = json.loads(line)
                logging.debug(f"Received request #{request_count}: method={request.get('method')}, id={request.get('id')}")

                # 添加全局异常保护
                try:
                    handle_request(request)
                    logging.debug(f"Request {request.get('id')} handled successfully")
                except Exception as e:
                    # 捕获所有未预料的异常，返回错误而不是崩溃
                    logging.error(f"handle_request failed: {e}\n{traceback.format_exc()}")
                    send_response({
                        'jsonrpc': '2.0',
                        'id': request.get('id'),
                        'error': {
                            'code': -32603,
                            'message': f'Internal server error: {str(e)}'
                        }
                    })

            except json.JSONDecodeError as e:
                logging.warning(f"JSON parse error: {e}")
                send_response({
                    'jsonrpc': '2.0',
                    'id': None,
                    'error': {
                        'code': -32700,
                        'message': f'Parse error: {str(e)}'
                    }
                })

    except KeyboardInterrupt:
        logging.info("MCP Server stopped by user (Ctrl+C)")
    except Exception as e:
        logging.critical(f"Fatal error in main loop: {e}\n{traceback.format_exc()}")
        raise
    finally:
        logging.info(f"=== MCP Server Stopped === (processed {request_count} lines)")
        logging.debug(f"stdin closed={sys.stdin.closed}, stdout closed={sys.stdout.closed}")
        logging.info("Main loop exited normally (EOF on stdin)")

if __name__ == '__main__':
    main()
