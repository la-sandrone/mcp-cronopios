# mcp-cronopios

> **奥雷里亚诺的小金鱼作坊** — *El taller de los pescaditos de oro*

五个 MCP 服务器，专治大语言模型最要命的几种"瞎编"。

---

## 为什么叫"小金鱼作坊"

《百年孤独》里，奥雷里亚诺·布恩迪亚上校晚年把自己关进作坊做小金鱼：做二十五条，
熔掉，再做二十五条。有人问他为什么，他说——**卖小金鱼是为了继续做小金鱼**。

这个仓库也是这么回事。这里的每一条"小金鱼"，都是被同一个问题逼出来的：

> **LLM 在你最需要它精确的地方，恰恰最爱瞎编。**

它算错数、编随机数、把符号移项搞得一团糟、生成代码先倒在一个语法错上、改文件时踩坏
编码和行号——而这些恰恰是**计算机四十年前就解决得很好的事**。所以这个小作坊做的事情
很朴素：**让 LLM 别再自己动手，把活交给真正的工具，它只管调用。**

（命名与同门 [mcp-melquiades](https://github.com/la-sandrone/mcp-melquiades)、
[mcp-pergaminos](https://github.com/la-sandrone/mcp-pergaminos)、
[memoria-del-hielo](https://github.com/la-sandrone/memoria-del-hielo) 同属拉美文学母题。
melquiades 是梅尔基亚德斯，pergaminos 是那卷羊皮纸，这里是做小金鱼的那个人。）

---

## 五条金鱼：缺陷 → 补丁

| 你的 LLM 会…… | 哪条金鱼 | 它替你做什么 |
|---|---|---|
| 算错数，还编出根本不存在的位数 | **mcp-calculator** | mpmath 任意精度求值 |
| 假装生成"随机数"（其实是伪随机，或者抄训练集里的数） | **mcp-entropia** | 13 种概率分布的真实采样 |
| 符号移项时幻觉变量、把常数改成"记得的"错值 | **mcp-eqsolve** | SymPy 符号求解 + 变量声明契约 |
| 生成代码，先倒在一个 L0 语法错上 | **mcp-pysyntaxcheck** | `ast` 只查语法，不猜 |
| 改文件时搞乱行号、踩坏编码、静默丢数据 | **fast_filesystem** | 行号坐标系 + 墓碑删除 |

---

## 每条金鱼

### 1. mcp-calculator — 任意精度数值求值

**它修补的缺陷**：LLM 不内置可靠的计算能力——它的算术输出是**查表**，不是推理。
所以它会给出一个"看起来很合理"的错答案，还带着自信的位数。

**设计要点**：

- **mpmath 任意精度**：默认 50 位有效数字，上限 10000 位。不是"够大就行"的浮点。
- **子进程隔离**：求值在 `python -I` 的隔离子进程里跑，父进程负责超时与回收——表达式再
  离谱也炸不到服务器本身。
- **AST 白名单**：只有"求值"类函数（三角、特殊函数、积分、求和、矩阵）进白名单；
  `findroot` / `odefun` / `solve` 这些"求解"类**一律排除**——那不是数值计算该干的事
  （符号求解见 eqsolve）。
- **资源红线**：指数上限、参数上限、AST 深度、内存 1 GiB，全部显式设死；
  `10**1000000` 这种表达式会被直接挡在门外。

### 2. mcp-entropia — 概率分布采样

**它修补的缺陷**：让 LLM"生成一个随机数"，它给你的是 token 采样——不是随机，是**概率最高的
那个"看起来随机"的数**。它会反复给 42、7、以及训练集里的常见模式。

**设计要点**：

- **13 种分布**：uniform / normal / lognormal / exponential / gamma / beta / binomial /
  poisson / chi_squared / bernoulli / randint 等，覆盖常见建模场景。
- **诚实的安全声明**：它**不是** CSPRNG（非密码学安全）。所有分布用 Python stdlib `random`
  （Mersenne Twister）或 NumPy 的 PCG-64。密钥、盐值、安全令牌请走 `secrets` 模块——
  这句话写在工具描述的第一行，不让任何人误用。
- 三个需要 NumPy 的分布（binomial / poisson / chi_squared）**可选依赖**，没有 NumPy 时
  只缺这三个，其余照常。

> 完整设计记——缘起（帮人核查卡方分布）、stdlib vs numpy 的选型、13 个工具怎么定下来的——
> 见 [`docs/entropia-design.md`](docs/entropia-design.md)。

### 3. mcp-eqsolve — 符号方程求解

**它修补的缺陷**：LLM 做符号操作是灾难现场——它会**幻觉出表达式里根本没有的变量**，移项时
悄悄丢符号，把常数替换成它"记得"的另一个值，然后给你一个推导过程完整、结论完全错误的解。

**设计要点**：

- **三步契约**：`declare_vars`（声明变量，带类型与域）→ `add_equation`（逐个加方程）→
  `solve`。**未声明的符号一律拒绝**——这一步专门堵"自创符号"这个幻觉源头。
- **SymPy 内核**：线性方程组走 `linsolve`，非线性走 `nonlinsolve`，单方程走 `solveset`，
  自动判型。
- **输出 LaTeX**：解以 LaTeX 返回，**供人审计**——不是让 LLM 转述，是让人一眼能核对。

### 4. mcp-pysyntaxcheck — Python 语法检查

**它修补的缺陷**：LLM 生成的代码，最廉价的失败方式是**语法错误**——引号不配对、缩进错位、
`"""` 状态机崩塌。这类错误不该等到运行时才发现，更不该由人肉去数括号。

**设计要点**：

- **只管语法**：用 `ast` 编译，只回答"这段代码在 Python 语法上成不成立"。**不查** import
  能不能找到、**不查**运行时错误、**不查** API 是否过时——边界写死在工具描述里，不越界。
- **顺带查编码**：PEP 263 编码声明、非 UTF-8 文件检测。
- **空文件 / 纯注释 / 纯 shebang** 都算合法（不误报）。

### 5. fast_filesystem — 文件读写 / 搜索 / 编码

**它修补的缺陷**：这是全作坊最"重"的一条。LLM 编辑文件时干的坏事不止一桩——

- **行号漂移**：它记着"第 42 行是 `foo`"，改完别处之后，第 42 行已经不是 `foo` 了。传统
  编辑器一动就重排行号，LLM 的上下文里那份行号表瞬间作废。
- **踩坏编码**：把 GBK 文件当 UTF-8 读、写入时把 BOM 丢掉、到处留 `?`。
- **静默丢数据**：行被删了就真没了，没人知道原来第几行有过什么。

**设计要点**：

- **行号坐标系 + 墓碑删除（仿 FAT32 的 0xE5）**：这是整个工具的灵魂。核心不变式只有一句——
  **行号是坐标系，换行符是坐标网格，不许偷渡换行符。** 删除不真删，而是原地填入"墓碑字节"
  （编码层的非法字节），**行数与行号一个都不变**，读的时候显示成 `[DELETED]`。要真正物理
  移除墓碑行，得显式调用 `cronopio_gc`。这样一来，LLM 拿到的行号永远不会"过一秒就失效"。
  （这个想法直接借自 FAT32：删除一个文件时，不擦除数据，只在目录项首字节盖上 `0xE5` 的
  删除标记。）
- **编码属性层**：BOM 显式建模（读剥写补）、无 BOM 时按确定性策略判（UTF-8 严格校验通过
  即 utf-8，否则 gb18030），**绝不自动采信无 BOM 的 UTF-16/32**（经典"联通"问题的坑）。
  编码不可表示时 **fail-loud**，绝不静默替换成 `?`。
- **搜索**：目录 grep 走 ripgrep 引擎（可选的 PyICU 做字素簇切分），flat scandir 默认、
  递归显式开启。
- **WSL 适配**：本项目源自 WSL 环境——针对 drvfs 跨文件系统搜索做了慢速提示，并提供
  `D:\foo\bar` ↔ `/mnt/d/foo/bar` 的路径自动归一化。Linux / macOS 下这些逻辑自然失效，
  无害。

> ⚠️ **使用前必读**：安全边界（`ALLOWED_ROOTS` / `SEARCHABLE_DIRS`）在文件顶部是
> **占位符，必须自己填**。不填会拒绝启动——宁可 fail-loud，也不留一个"静默允许任意目录"
> 的默认值。

---

## 快速上手

每件都是**单文件 MCP 服务器**，用 [PEP 723](https://peps.python.org/pep-0723/) 内联依赖声明，
装了 `uv` 就能直接跑；也可以放进任意 Python ≥ 3.12 环境，装好对应的依赖包再 `python` 起。

```bash
# 方式一：uv 直接跑（自动读文件头的 inline dependencies）
uv run servers/mcp_calculator.py

# 方式二：普通环境
pip install "mcp[cli]" mpmath
python servers/mcp_calculator.py
```

各件的依赖：

| 件 | 依赖 |
|---|---|
| mcp-calculator | `mcp`, `mpmath` |
| mcp-entropia | `mcp`（+ `numpy` 可选，用于 3 个分布） |
| mcp-eqsolve | `mcp`, `sympy` |
| mcp-pysyntaxcheck | `mcp` |
| fast_filesystem | `mcp`；`ripgrep`（外部二进制）用于目录 grep；`PyICU` 可选（字素簇切分） |

所有服务器都走 **stdio** 传输——在 MCP 客户端里按标准 stdio server 配置即可。
mcp 1.x / 2.x 都支持（文件内有兼容层）。

客户端配置示例（mcp-calculator）：

```json
{
  "mcpServers": {
    "mcp-calculator": { "command": "uv", "args": ["run", "/abs/path/servers/mcp_calculator.py"] }
  }
}
```

更细的部署经验（venv 前提、启动器写法、踩过的坑）见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 测试

两层，分工明确：

- **`tests/smoke_test.py`** —— 静态冒烟，**零依赖**（不 import mcp/sympy/mpmath）。查结构、
  工具名前缀、占位符、语法。任何环境都能跑。
- **`tests/functional_test.py`** —— 功能级，**真起 stdio 服务器、发 JSON-RPC 调工具**。
  需要装了依赖的解释器。

```bash
python3 tests/smoke_test.py          # 零依赖，随时可跑
python3 tests/functional_test.py     # 需 mcp / sympy / mpmath / numpy
```

`functional_test.py` 对 `fast_filesystem` 会自动生成"占位符已填"的临时副本再测
（原文件有启动守卫，不填会拒启动）。

## 设计出处

每件工具背后都有具体的场景与教训——为什么这样切分、哪个坑逼出了哪条规则、
哪些"看起来能用"的写法被实测证伪。这些**比代码本身更重要**，集中在
[`DESIGN.md`](DESIGN.md)。

## 许可

**AGPL-3.0**（见 [LICENSE](LICENSE)）。

这是一个**自用娱乐作品**，丑话说在前：

- 不为任何人做兼容性承诺，接口随我高兴改；
- `mcp-entropia` 明确**非密码学安全**，别拿来生成密钥；
- `fast_filesystem` 的墓碑删除体系有**真实的数据危险**——它会真的物理删除东西，请先在
  不重要的目录上试明白再用。

觉得有用就拿去改，但别指望它对你的数据负责。
