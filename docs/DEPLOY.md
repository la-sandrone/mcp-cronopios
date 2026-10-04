# 部署经验（实测踩坑）

> 这里记的是"跑起来"层面的坑，不是设计层面（后者见 [`../DESIGN.md`](../DESIGN.md)）。
> 全部来自真实部署。

## 1. 依赖前提：PEP 723 不是万能的

文件头的 `# /// script` 内联依赖**只在 `uv run` 时生效**。如果你用普通 Python 起
（比如 `python servers/mcp_calculator.py`），PEP 723 注释**不起任何作用**——依赖必须
已经装在当前环境里。

`mcp-calculator` 尤其要注意：它的 `worker.py` 是用 `sys.executable -I` 起的**子进程**，
只会加载**当前解释器所属环境**里的包。所以父进程和子进程必须共用同一个装了 `mpmath`
的环境，否则子进程直接 import 失败。

## 2. 常驻进程**不热加载**

MCP 服务器是常驻进程。**改了源码必须重启服务器**才生效——它不会自动重载。
（这条在开发时最容易忘，症状是"我明明改了，怎么还是老行为"。）

离线自测不受影响：`mcp-calculator` 的 `worker.py --selftest` 可以直接跑，不用重启。

## 3. 启动器写法：`-l` 不要写成 `-lc`

用 `wsl.exe bash -l <启动器> <server-name>` 这种形式时，**必须用 `-l`，不能用 `-lc`**。
某些客户端下 `-c` 形式会解析失败（实测日期 2026-07-31）。

## 4. 命令里的引号会被吃掉

某些 MCP 客户端（实测 一些古老版本的 Chatbox）**会把命令参数里的引号吃掉**，导致路径解析失败。
配置启动命令时**不要带引号**，把路径里的空格用别的方式规避（或把启动器放在无空格路径下）。

## 5. 客户端传参不看 schema 类型

线上实测：把一个参数声明成 `str`，LLM 客户端直接传了 `dict` 进来，被 pydantic 拒绝。
**不要假设调用方遵守你的类型标注**——该放宽的地方（如 `Union[str, dict]`）就放宽。
这是适配真实调用方，不是妥协。

## 6. `fast_filesystem` 的启动前检查

它是唯一一个**必须先配置才能启动**的成员：

1. 打开 `servers/fast_filesystem.py`，填文件顶部的 `ALLOWED_ROOTS` 与 `SEARCHABLE_DIRS`
   （占位符 `<root-1>` 等）；
2. 不填会**拒绝启动**并报出未填的占位符——这是故意的，不留"静默允许任意目录"的默认值；
3. 目录 grep 需要系统里有 `ripgrep`（`rg`）可执行文件；
4. 需要字素簇精确切分时，装 `PyICU`（可选）。

## 7. 最小启动器模板

一个简单的 stdio 启动器（Linux / WSL）：

```bash
#!/usr/bin/env bash
# 用法: launcher.sh <server-basename>
set -euo pipefail
SERVER="$1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/servers/${SERVER}.py"
```

客户端配置里把 `command` 指向这个启动器或直接指向 `uv` / `python3` 即可。

## 8. 另一个热修改大坑

```python
if __name__ == "__main__":
    mcp.run()

```

这种典型语句**必须出现在程序最后**，要不然**它下面所有东西都会被忽略——追加模式典型bug**。
