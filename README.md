# Claude Code MCP Async Server

**Asynchronous MCP wrapper for Claude Code CLI**

Enable Claude Code to spawn child Claude Code sessions for parallel task execution.

## Features

- ✅ **Async execution** - Start tasks in background, continue working
- ✅ **Multi-instance parallelism** - Run multiple Claude Code sessions simultaneously
- ✅ **Automatic cleanup** - No zombie processes
- ✅ **Zero config** - Works out of the box

## Quick Start

### 1. Install

```bash
git clone https://github.com/jeanchristophe13v/claudecode-mcp-async.git
cd claudecode-mcp-async
chmod +x claudecode_mcp_async_server.py
```

### 2. Configure Claude Code

Add to your `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "claude-code-mcp": {
      "command": "/absolute/path/to/claudecode-mcp-async/claudecode_mcp_async_server.py",
      "args": [],
      "env": {}
    }
  }
}
```

> **Tip**: Use absolute path. Replace `/absolute/path/to/` with your actual path.

### 3. Restart Claude Code

Reload or restart Claude Code to load the MCP server.

## Usage

### Example: Async execution (recommended)

The key advantage is **non-blocking execution** - start a task and continue working immediately.

```python
# Start a long-running task in background
task = claude_code_execute_async(
    prompt="Analyze all Python files and generate a comprehensive report",
    working_dir="/path/to/project",
    skip_permissions=True
)
# ✅ Returns immediately with Task ID: abc12345

# Continue your work while Claude Code runs in background
# ... do other things ...

# Check result when ready
result = claude_code_check_result(task_id="abc12345")
```

### Example: Parallel execution

Run multiple tasks simultaneously:

```python
# Start multiple tasks at once
task1 = claude_code_execute_async(
    prompt="Generate unit tests for utils.py"
)

task2 = claude_code_execute_async(
    prompt="Refactor database.py to use async/await"
)

task3 = claude_code_execute_async(
    prompt="Add type hints to all functions in api.py"
)

# All three tasks run in parallel
# Check results when ready
result1 = claude_code_check_result(task_id=task1)
result2 = claude_code_check_result(task_id=task2)
result3 = claude_code_check_result(task_id=task3)
```

### Example: Synchronous execution

For simple tasks that need immediate results:

```python
result = claude_code_execute(
    prompt="Write a Python function to validate email addresses",
    skip_permissions=True
)
# ⏳ Blocks until completion, then returns result
```

## API Reference

### `claude_code_execute_async`
Start a task in background, return immediately.

**Parameters:**
- `prompt` (required): Task description
- `working_dir` (optional): Working directory
- `model` (optional): "sonnet", "opus", or "haiku"
- `skip_permissions` (optional): Skip permission checks (default: true)

**Returns:** Task ID string

### `claude_code_check_result`
Check async task status.

**Parameters:**
- `task_id` (required): Task ID from `claude_code_execute_async`

**Returns:**
- `running`: Task in progress
- `completed`: Task finished with result

### `claude_code_execute`
Synchronous execution (blocks until completion).

**Parameters:**
- `prompt` (required): Task description
- `working_dir` (optional): Working directory
- `model` (optional): "sonnet", "opus", or "haiku"
- `timeout` (optional): Timeout in seconds
- `skip_permissions` (optional): Skip permission checks (default: true)

**Returns:** Task result

## Why Async?

**Problem:** Claude Code blocks the parent session while running.

**Solution:** This MCP server spawns child Claude Code processes that run in the background.

**Benefits:**
- 🚀 Start a task and continue working immediately
- ⚡ Run multiple tasks in parallel
- 🎯 No blocking, no waiting
- 🧹 Automatic process cleanup

## Troubleshooting

**Server not showing up?**
- Use absolute path in config
- Run: `chmod +x claudecode_mcp_async_server.py`
- Restart Claude Code

**Task stuck in "running"?**
- Wait a moment, large tasks take time
- Check: `ls -la /tmp/claude_code_tasks/`
- View logs: `tail -f /tmp/claude_code_mcp_debug.log`

## Requirements

- Python 3.6+
- Claude Code CLI installed

## License

MIT License

---

**Questions?** Open an issue on GitHub.

