# 环境搭建：系统依赖、Python 安装与自检

## 1. 本讲目标

上一讲我们知道了 olmOCR 是什么。这一讲解决一个非常实际的问题：**把它装到你的电脑上，并确认装对了**。

学完本讲，你应该能够：

1. 说出 olmOCR 的四种安装形态（轻量远程推理 / 本地 GPU / Beaker 集群 / benchmark 套件）各自适合什么场景。
2. 读懂 `pyproject.toml`，说清楚 `pip install olmocr` 到底装了什么、**没装**什么。
3. 逐行读懂 `olmocr/check.py` 的三个自检函数，理解「系统依赖 poppler」为什么不能用 pip 安装。
4. 独立完成一套干净的 conda 环境 + 系统依赖 + 轻量安装，并会用 `python -m olmocr.check` 和退出码判断环境状态。

## 2. 前置知识

本讲会用到几个 Python 打包和系统运维的基础概念，先用大白话过一遍：

- **系统依赖 vs Python 依赖**：`pip` 只能安装 Python 包。像 `pdftoppm` 这种编译好的 C/C++ 系统程序（属于 poppler 项目），必须用系统的包管理器（Ubuntu 的 `apt-get`、macOS 的 `brew`）安装。olmOCR 两类依赖都要，这是新手最容易踩的第一个坑。
- **poppler 是什么**：一个经典的 PDF 渲染工具库。olmOCR 在把 PDF 交给视觉模型之前，要先用它自带的 `pdftoppm` 把每一页渲染成 PNG 图片——模型「看」的其实是这张图片，不是 PDF 本身。所以 poppler 装不好，整条流水线第一步就断了。
- **pip extras（可选依赖组）**：`pip install olmocr[gpu]` 里的方括号就是 extras。它对应 `pyproject.toml` 里 `[project.optional-dependencies]` 下的某个列表，意思是「在基础依赖之外，再装这一组」。不写方括号就只装基础依赖。
- **conda 环境**：一个独立的 Python 解释器 + 包集合的「沙盒」。不同项目隔离，互不污染。
- **退出码（exit code）**：程序结束时返回给操作系统的一个整数，0 表示成功，非 0 表示失败。在 shell 里用 `echo $?` 可以查看上一条命令的退出码。
- **logging 的默认行为**：Python 的 `logging` 模块默认只输出 WARNING 及以上级别的日志；代码里写的 `logger.info(...)` 如果不配置日志级别，是**看不到**的。这一点在阅读 `check.py` 时很关键。
- **`importlib.util.find_spec`**：只检查「某个模块能不能被找到」，但**不真正导入它**。适合探测可选的重型依赖是否存在，避免探测动作本身触发几十秒的加载。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml) | 项目的打包清单：基础依赖、五个可选依赖组（gpu/beaker/dev/bench/train/elo）、`olmocr` 命令行入口的定义都在这里。 |
| [olmocr/check.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py) | 环境自检脚本，`python -m olmocr.check` 即运行它。检查 poppler 是否可用、sglang 是否安装，另含一个未被主流程调用的 GPU 显存检查函数。 |
| [README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md) | 安装说明的「官方答案」：系统依赖 apt 命令、conda 建环境步骤、四种安装选项和故障排查。 |

## 4. 核心概念与源码讲解

### 4.1 pyproject.toml 依赖与 extras

#### 4.1.1 概念说明

一个 Python 项目的「门牌」和「购物清单」都写在 `pyproject.toml` 里。对 olmOCR 来说，这份文件回答三个问题：

1. **基础安装装什么**（`[project.dependencies]`）——不管你怎么用 olmOCR，这些都得装。
2. **可选组装什么**（`[project.optional-dependencies]`）——按使用场景追加，比如本地跑模型才需要 PyTorch。
3. **`olmocr` 这个命令从哪来**（`[project.scripts]`）——安装后终端里能敲的 `olmocr` 命令，就是在这里注册的。

olmOCR 刻意把「重」的依赖全部排除在基础安装之外：基础列表里没有任何深度学习框架，最大的包也就几 MB 到几十 MB。而 GPU 推理需要的 `torch`（约 2GB+）、`vllm` 都放在 `gpu` extra 里。这样只想**调用远程服务器**做 OCR 的用户，不必在本机下载几个 GB 的 CUDA 生态。

#### 4.1.2 核心流程

`pip install olmocr[...]` 时 pip 的决策流程可以概括为：

```text
读取 pyproject.toml
  ├─ 解析 requires-python：本机 Python 必须 >= 3.11，否则直接报错
  ├─ 安装 [project.dependencies] 基础列表（17 个包，全部是轻量纯 Python / 小型库）
  ├─ 如果用户写了 extras（如 olmocr[gpu]）
  │    └─ 追加安装对应可选列表（gpu → torch + transformers + vllm）
  ├─ 注册 [project.scripts] 入口 → 在 bin/ 目录生成可执行的 olmocr 命令
  └─ 动态读取 olmocr.version.VERSION 作为版本号
```

#### 4.1.3 源码精读

**（1）Python 版本门槛**。[pyproject.toml:L21](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L21) 声明 `requires-python = ">=3.11"`，也就是说用 Python 3.10 或更老版本安装会直接失败。这也是实践环节选 3.11 的原因。

**（2）基础依赖列表**。[pyproject.toml:L22-L40](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L22-L40) 定义了轻量安装的全部 Python 依赖，按用途归类如下：

| 用途 | 包 | 在 olmOCR 里的角色 |
| --- | --- | --- |
| PDF 处理 | `pypdf>=5.2.0`、`pypdfium2` | pypdf 读写 PDF（页数统计等）；pypdfium2 是 Google PDFium 的绑定，是提取 PDF 内置文本的引擎之一 |
| 存储 / IO | `cached-path`、`smart_open`、`boto3`、`zstandard`、`orjson`、`requests`、`httpx` | 缓存远程模型文件；透明打开本地/S3 文件；AWS S3 SDK；zstd 压缩（工作队列索引文件）；高速 JSON；HTTP 客户端 |
| 文本处理 | `ftfy`、`bleach`、`markdown2`、`markdownify`、`lingua-language-detector` | 修复乱码 Unicode；HTML 消毒；Markdown↔HTML 互转（`markdownify` 在合成数据模块 `olmocr/synth/` 中使用）；语言检测（`olmocr/filter/` 中过滤非目标语言 PDF） |
| 图像 / 其他 | `Pillow`、`filelock`、`cryptography` | 图像读写；文件锁；加密后端（pypdf 打开加密 PDF 时需要） |

注意这个列表里**没有** torch、vllm、transformers——这就是「轻量」的含义。

**（3）命令行入口**。[pyproject.toml:L49-L50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) 写着：

```toml
[project.scripts]
olmocr = "olmocr.pipeline:cli_main"
```

安装后 pip 会在环境的 `bin/` 下生成一个 `olmocr` 可执行脚本，它做的事情等价于调用 `olmocr.pipeline` 模块里的 `cli_main` 函数。所以 `olmocr ...` 和 `python -m olmocr.pipeline ...` 两种写法效果相同（README 也提到了这一点）。`pipeline.py` 的内部结构是第 2 单元的主角，本讲只需记住入口指向它。

**（4）可选依赖组**。集中定义在 [pyproject.toml:L52-L125](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L52-L125)：

| extra | 行号 | 内容 | 什么时候装 |
| --- | --- | --- | --- |
| `gpu` | [L53-L57](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L53-L57) | `torch>=2.7.0`、`transformers==4.57.3`、`vllm==0.11.2` | 本地 GPU 起推理服务（注意 transformers 和 vllm 是**精确钉死版本**，这是官方强调干净环境的根源） |
| `beaker` | [L59-L61](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L59-L61) | `beaker-py` | 用 `--beaker` 把任务提交到 AI2 的 Beaker 集群 |
| `dev` | [L63-L90](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L63-L90) | ruff/mypy/pytest 等开发工具 | 参与开发、跑测试 |
| `bench` | [L92-L106](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L92-L106) | playwright、openai、anthropic、flask 等 | 跑 olmOCR-Bench 评测（第 5 单元） |
| `train` | [L108-L118](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L108-L118) | torch、trl、peft、wandb 等 | 微调训练（第 6 单元） |
| `elo` | [L120-L125](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L120-L125) | numpy、scipy、pandas、matplotlib | 评测数据的统计分析 |

**（5）动态版本号**。[pyproject.toml:L149-L150](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L149-L150) 用 `dynamic = ["version"]` + `version = {attr = "olmocr.version.VERSION"}` 把版本号指向 `olmocr/version.py` 里的常量，打包时动态读取。

#### 4.1.4 代码实践

**实践目标**：用 pip 的元数据命令验证「轻量安装确实没装重型依赖」。

**操作步骤**（在任一已执行 `pip install olmocr` 的环境中）：

```bash
# 1. 查看 olmocr 的版本与依赖声明
pip show olmocr

# 2. 列出当前环境里与深度学习相关的包
pip list | grep -i -E "torch|vllm|transformers|beaker|playwright"

# 3. 查看 pip 记录的 extras（较新版本 pip 支持）
pip show olmocr | grep -i requires
```

**需要观察的现象**：第 2 步的 grep 应当**没有任何输出**（或匹配不到 torch/vllm 等）。

**预期结果**：`pip show olmocr` 的 `Requires` 字段列出的是基础依赖（pypdf、boto3、Pillow 等）；环境里搜不到 torch。如果搜到了，说明你（或某个工具）之前在这个环境装过 GPU 相关的包，环境不够「干净」。

**待本地验证**：不同版本 pip 的 `pip show` 输出格式略有差异，以你本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install olmocr` 不会安装 PyTorch？

**答案**：PyTorch（`torch>=2.7.0`）位于 `[project.optional-dependencies]` 的 `gpu` 列表（[pyproject.toml:L53-L57](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L53-L57)），只有显式写 `pip install olmocr[gpu]` 才会追加安装；基础 `dependencies` 列表（L22-L40）中没有它。

**练习 2**：执行 `pip install olmocr[gpu,bench]` 会安装哪些包？

**答案**：基础依赖 ∪ `gpu` 组（torch、transformers、vllm）∪ `bench` 组（playwright、openai、anthropic、flask 等）。extras 之间逗号分隔即取并集。

**练习 3**：安装完成后，终端里的 `olmocr` 命令是哪来的？

**答案**：由 [pyproject.toml:L49-L50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) 的 `[project.scripts]` 声明，pip 安装时在环境 `bin/` 目录生成入口脚本，指向 `olmocr.pipeline:cli_main` 函数。

### 4.2 check.py 环境自检

#### 4.2.1 概念说明

`olmocr/check.py` 是官方提供的环境「体检脚本」，用 `python -m olmocr.check` 运行。它解决的问题是：olmOCR 的成败依赖一堆**环境之外**的东西（系统程序、GPU、其他服务），任何一环缺失都会让流水线在运行中段以更难懂的方式崩溃。与其跑到一半失败，不如开工前先逐项检查、缺什么直接报错退出。

这个文件很小，但浓缩了三个值得学的工程手法：用 `subprocess` 探测系统程序、用 `find_spec` 探测 Python 包、用 `sys.exit(1)` 向 shell 传递失败信号。

还有一个源码考古级的彩蛋：脚本里有一个检查 **sglang** 的函数——而 README 的 News 明确记录 v0.1.75（2025 年 6 月）已把推理引擎从 sglang 换成 vLLM（[README.md:L44](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L44)）。这是历史遗留代码，读源码时要能识别这种情况：**代码库里存在 ≠ 正在使用**。

#### 4.2.2 核心流程

`python -m olmocr.check` 的执行流程：

```text
__main__ 入口
  ├─ check_poppler_version()
  │    ├─ subprocess 运行 ["pdftoppm", "-h"]
  │    ├─ returncode == 0 且 stderr 以 "pdftoppm" 开头 → 记 INFO 日志，通过
  │    ├─ 命令存在但条件不满足 → 记 error 日志 → sys.exit(1)
  │    └─ 抛 FileNotFoundError（系统里没有这个程序）→ 记 error 日志 → sys.exit(1)
  └─ check_sglang_version()
       ├─ find_spec("sglang") 找得到 → 通过（无输出）
       └─ 找不到 → 记两条 error 日志 → sys.exit(1)
```

注意：`check_torch_gpu_available` 虽然定义了，但**不在** `__main__` 的调用链里（详见 4.2.3 第 4 点）。另外整个脚本没有调用 `logging.basicConfig`，所以 INFO 级别的「成功」日志默认是看不到的——你能看到的只有 ERROR 级别的失败信息和退出码。

#### 4.2.3 源码精读

**（1）poppler 检查——如何探测一个系统程序**。[check.py:L9-L20](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L9-L20)：

```python
def check_poppler_version():
    try:
        result = subprocess.run(["pdftoppm", "-h"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and result.stderr.startswith("pdftoppm"):
            logger.info("pdftoppm is installed and working.")
        else:
            logger.error("pdftoppm is installed but returned an error.")
            sys.exit(1)
    except FileNotFoundError:
        logger.error("pdftoppm is not installed.")
        ...
        sys.exit(1)
```

这段代码区分了三种状态：**没装**（`FileNotFoundError`——shell 找不到 `pdftoppm` 这个命令）、**装了但行为异常**（退出码非 0 或 stderr 开头不对）、**正常**。判定依据是运行 `pdftoppm -h` 后「退出码为 0 且 stderr 以 `pdftoppm` 开头」——poppler 系工具会把版本帮助打印到 stderr。两个失败分支都以 `sys.exit(1)` 结束，把失败传给 shell。

**（2）sglang 检查——不导入也能探测**。[check.py:L23-L27](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L23-L27)：

```python
def check_sglang_version():
    if importlib.util.find_spec("sglang") is None:
        logger.error("Please make sure sglang is installed ...")
        sys.exit(1)
```

`find_spec` 只回答「这个包在不在」，不执行包的顶层代码。对 sglang 这种导入开销很大的推理框架，这种探测方式不会拖慢自检。错误信息还提示 sglang 需要按其官方文档单独安装（因为它有自己的依赖解析要求）。

**（3）GPU 检查——当前是「孤儿函数」**。[check.py:L30-L42](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L30-L42)：

```python
def check_torch_gpu_available(min_gpu_memory: int = 15 * 1024**3):
    ...
    gpu_memory = torch.cuda.get_device_properties(0).total_memory
    assert gpu_memory >= min_gpu_memory
```

它检查 torch 可导入、且 0 号卡总显存不低于默认阈值 \(15 \times 1024^3\) 字节（即 15 GiB）。git 历史显示这个阈值曾被下调（提交信息 "Lowered memory usage check per #290"）。但要注意：[check.py:L45-L47](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L45-L47) 的 `__main__` 块只调用了 `check_poppler_version()` 和 `check_sglang_version()`，**并没有调用它**。所以即使装好了 GPU 环境，`python -m olmocr.check` 也不会替你检查显存；想检查得自己 import 这个函数调用。另外 README 对本地 GPU 的建议是「至少 12 GB 显存」（[README.md:L221](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L221)），与本函数 15 GiB 的默认阈值并不一致——以代码为准的同时，也要意识到文档和代码可能短暂脱节。

**（4）入口块**。[check.py:L45-L47](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L45-L47) 就是两行顺序调用，任何一个检查失败都会 `sys.exit(1)` 中断后续检查。这也意味着：如果 poppler 没装，你甚至走不到 sglang 检查那一步。

#### 4.2.4 代码实践

**实践目标**：亲手跑一次自检脚本，并用「单函数调用」绕过 sglang 检查单独验证 poppler。

**操作步骤**：

```bash
# 1. 直接感受一下 pdftoppm（先确认系统里有没有它）
pdftoppm -h

# 2. 运行官方自检，并查看退出码
python -m olmocr.check
echo "exit code: $?"    # Windows PowerShell 用 $LASTEXITCODE

# 3. 只调用 poppler 检查，并打开 INFO 日志让「成功」也可见
python -c "import logging; logging.basicConfig(level=logging.INFO); from olmocr.check import check_poppler_version; check_poppler_version(); print('poppler check passed')"
```

**需要观察的现象**：

- 第 2 步在「轻量安装 + 已装 poppler」的环境里，大概率会打印 sglang 相关的两行 error 并以退出码 1 结束——这是**预期行为**，因为当前 HEAD 的自检脚本仍在检查 sglang，而轻量模式（乃至任何 extra）都不会装它；
- 第 3 步应打印 `pdftoppm is installed and working.`（INFO 日志，因我们配置了 basicConfig）和 `poppler check passed`。

**预期结果**：第 3 步正常结束、退出码 0，即可确认 poppler 这一项过关；第 2 步的失败只说明 sglang 项不过关（见 4.2.3 的历史遗留分析）。

**待本地验证**：`pdftoppm -h` 的退出码在不同 poppler 版本上表现可能不同（部分版本对 `-h` 返回非 0）。如果你在第 3 步看到的是 `pdftoppm is installed but returned an error.`，说明你的 poppler 版本属于「帮助信息打印了但退出码非 0」的情况——请把你机器上的实际输出和 `pdftoppm -v` 的版本号记录下来，这是判断该检查在你的环境是否可靠的第一手证据。

#### 4.2.5 小练习与答案

**练习 1**：`check_poppler_version` 如何区分「没安装」和「安装了但坏了」两种失败？

**答案**：没安装时 `subprocess.run` 抛 `FileNotFoundError`（进入 except 分支）；安装了但行为不符预期时走 else 分支（退出码非 0 或 stderr 前缀不对），报 `installed but returned an error`。两种失败最终都 `sys.exit(1)`。

**练习 2**：为什么 `check_sglang_version` 用 `importlib.util.find_spec("sglang")` 而不是直接 `import sglang`？

**答案**：`find_spec` 只定位模块、不执行模块代码。sglang 导入开销大且其官方文档要求单独安装以正确解析依赖，探测式检查既快又避免导入副作用。

**练习 3**：装有 12 GiB 显存 GPU 的机器上运行 `python -m olmocr.check` 全部通过，能说明显存够用吗？

**答案**：不能。`check_torch_gpu_available`（阈值 15 GiB）虽然定义在 [check.py:L30](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L30)，但 `__main__` 并不调用它；脚本通过只代表 poppler 与 sglang 两项通过。

### 4.3 conda 干净环境实践

#### 4.3.1 概念说明

README 用加粗语气强调：**务必在干净的 Python 环境里安装 olmOCR**（[README.md:L200-L201](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L200-L201)：*"The requirements for running olmOCR are difficult to install in an existing python environment"*）。原因在 4.1 已经埋下伏笔：`gpu` extra 里 `transformers==4.57.3`、`vllm==0.11.2` 都是**精确版本**，与已有环境中的其他包极易冲突。conda 环境的作用就是给 olmOCR 一块自留地。

同时要理解「系统依赖」这一层：conda 管不了 `pdftoppm`，它必须由 `apt-get`（或 brew 等）装到操作系统里；字体包则保证 pdftoppm 渲染 PDF 时能找到正确的字形——**模型看到的是渲染出来的图片**，字体缺漏会直接改变图片长相，进而影响识别质量。

#### 4.3.2 核心流程

从零到「可轻量使用」的完整流程（与 README 一致）：

```text
① apt 安装系统依赖（poppler-utils + 5 个字体包）
② conda create -n olmocr python=3.11 && conda activate olmocr
③ pip install olmocr            # 轻量：无 GPU 依赖
④ （可选自检）python -m olmocr.check / 单函数调用
⑤ 之后配合远程 --server 使用；本地 GPU 则改用 olmocr[gpu]
```

#### 4.3.3 源码精读

**（1）系统依赖的官方清单**。[README.md:L192-L196](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L192-L196)：

```bash
sudo apt-get update
sudo apt-get install poppler-utils ttf-mscorefonts-installer msttcorefonts fonts-crosextra-caladea fonts-crosextra-carlito gsfonts lcdf-typetools
```

`poppler-utils` 提供 `pdftoppm`（渲染）和 `pdftotext`/`pdfinfo`（提取文本、读页面尺寸——后两者在流水线里同样会用到）；其余全是字体包：微软核心字体、Caladea/Carlito（度量兼容 Cambria/Calibri 的开源字体）、Ghostscript 字体等。

**（2）conda 建环境**。[README.md:L203-L206](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L203-L206)：

```bash
conda create -n olmocr python=3.11
conda activate olmocr
```

选 3.11 恰好满足 `>=3.11` 的门槛，也是项目 CI/ruff 配置的目标版本（`target-version = "py311"`，[pyproject.toml:L178](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L178)）。

**（3）四种安装选项的原文位置**：

- 轻量远程推理：[README.md:L210-L216](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L210-L216)，`pip install olmocr`，明确说了避免安装 PyTorch（~2GB+）；
- 本地 GPU：[README.md:L218-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L218-L230)，`pip install olmocr[gpu] --extra-index-url https://download.pytorch.org/whl/cu128`，并推荐追加 flashinfer 加速；要求 NVIDIA GPU ≥12GB 显存 + 30GB 磁盘；
- Beaker：[README.md:L232-L237](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L232-L237)，`pip install olmocr[beaker]`；
- benchmark：[README.md:L239-L244](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L239-L244)，`pip install olmocr[bench]`。

**（4）故障排查**。[README.md:L257-L262](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L257-L262)：批处理大量 PDF 时可能报 `too many open files`，解法是 `ulimit -n 65536` 提高进程可打开文件数上限——olmOCR 是高并发流水线，会同时打开大量文件描述符。

#### 4.3.4 代码实践

**实践目标**：不装 GPU 依赖，验证轻量安装的「体积优势」，直观体会两种安装的差别。

**操作步骤**：

```bash
# 在干净环境中执行轻量安装，并记录耗时与体积
pip install olmocr

# 查看 pip 缓存里刚下载的包总体积（近似安装体积）
pip cache dir          # 打印缓存目录
du -sh $(pip cache dir) 2>/dev/null || true

# 对照：只解析不安装 GPU 版，看会引入哪些包（dry-run）
pip install --dry-run olmocr[gpu] 2>/dev/null | tail -20
```

**需要观察的现象**：轻量安装通常十几秒内完成；`--dry-run` 会列出 torch、nvidia 系 CUDA 库、vllm 等一大串「将要安装」的包（仅解析不落盘）。

**预期结果**：两组清单长度对比悬殊，这正是 README 把 GPU 依赖拆成 extra 的原因。

**待本地验证**：`--dry-run` 需要 pip ≥ 22.2；`du` 统计的缓存体积与你 pip 版本、缓存策略有关，数字仅供对比。

#### 4.3.5 小练习与答案

**练习 1**：为什么官方反复强调「干净环境」？

**答案**：`gpu` extra 精确钉死了 `transformers==4.57.3` 与 `vllm==0.11.2`（[pyproject.toml:L53-L57](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L53-L57)），在有其他深度学习项目的旧环境里极易产生版本冲突；conda 独立环境从根上避免。

**练习 2**：`ttf-mscorefonts-installer` 这些字体包和 OCR 准确率有什么关系？

**答案**：pdftoppm 渲染 PDF 页面时需要系统字体；缺字体时用回退字体渲染，图片与原版面出现偏差，而 VLM 识别的输入正是这张图片，所以字体缺漏会直接降低转写质量。

**练习 3**：批处理时报 `too many open files` 该怎么办？

**答案**：执行 `ulimit -n 65536` 提高文件描述符上限（[README.md:L259-L262](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L259-L262)）。

## 5. 综合实践

把本讲三个模块串起来，完成一次**从零到可运行**的轻量环境搭建。全程不需要 GPU。

**任务**：搭建 olmocr 轻量环境并用源码知识解释每一步。

**步骤**：

1. **系统依赖**（Ubuntu/Debian；macOS 用 `brew install poppler` 替代）：

   ```bash
   sudo apt-get update
   sudo apt-get install poppler-utils ttf-mscorefonts-installer msttcorefonts fonts-crosextra-caladea fonts-crosextra-carlito gsfonts lcdf-typetools
   ```

2. **干净 conda 环境**：

   ```bash
   conda create -n olmocr python=3.11
   conda activate olmocr
   ```

3. **轻量安装**：

   ```bash
   pip install olmocr
   ```

4. **自检**：运行 `python -m olmocr.check` 记录输出与退出码；再用下面命令单独验证 poppler：

   ```bash
   python -c "import logging; logging.basicConfig(level=logging.INFO); from olmocr.check import check_poppler_version; check_poppler_version(); print('OK')"
   ```

5. **对照源码写结论**：打开 [pyproject.toml:L52-L125](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L52-L125)，用 `pip list` 逐项核对，填写下面这张表：

   | 包 | 所属 extra | 本次是否已安装 |
   | --- | --- | --- |
   | torch | gpu | 应为「否」 |
   | vllm | gpu | 应为「否」 |
   | transformers | gpu | 应为「否」 |
   | beaker-py | beaker | 应为「否」 |
   | playwright | bench | 应为「否」 |
   | pypdf | （基础） | 应为「是」 |
   | boto3 | （基础） | 应为「是」 |

6. **验证命令入口**：运行 `which olmocr`，再运行 `olmocr --help` 能否打印帮助（此时即使没有 GPU 也能看到完整参数列表；参数含义是 u1-l5 的内容）。

**预期结果**：poppler 单项检查通过（第 4 步第二条命令退出码 0）；表格中「否」全部成立；`olmocr --help` 正常输出。第 4 步第一条命令在 sglang 检查处退出码为 1 属于预期（见 4.2 的分析），请把实际输出记进你的学习笔记。

**说明**：此环境尚不能本地推理（没有模型与 vLLM），但已可用于第 3 讲配合 `--server` 远程服务器的用法，或先跑通工作目录与队列相关的小实验。

## 6. 本讲小结

- olmOCR 的安装分两层：**系统依赖**（poppler-utils + 字体，用 apt/brew 装）和 **Python 依赖**（用 pip 装），缺第一层流水线的渲染步骤就会失败。
- `pip install olmocr` 是轻量安装：17 个基础包里没有任何深度学习框架；`torch/vllm/transformers` 在 `gpu` extra，`beaker-py` 在 `beaker` extra，评测和训练各有对应 extra——**按需选组**是它的安装哲学。
- `olmocr` 命令来自 `[project.scripts]` 的 `olmocr = "olmocr.pipeline:cli_main"`，与 `python -m olmocr.pipeline` 等价。
- `check.py` 的三个检查各有看点：poppler 检查展示 `subprocess` 探测系统程序的三态判定；sglang 检查用 `find_spec` 做无副作用探测，且是 v0.1.75 引擎切换前的**历史遗留**（当前 `__main__` 仍会调用它）；GPU 显存检查（15 GiB 阈值）定义了但未被调用。
- 阅读这类工程化脚本时，要主动核对 `__main__` 实际调用了什么、git 历史解释了什么，而不是看到函数就默认它在运行。
- 官方强调干净环境，根源是 gpu extra 对 transformers/vllm 的精确版本钉死；`ulimit -n 65536` 是高并发批处理的常见急救。

## 7. 下一步学习建议

环境就绪后，下一讲 **u1-l3（第一次运行：把一个 PDF 转成 Markdown）** 将用这套环境跑通第一条真实命令（配合远程 `--server` 或 GPU 机器），观察 workspace 目录里的产物。如果你想先搞清 `olmocr --help` 里那几十个参数分别控制什么，可以先跳到 **u1-l5（CLI 参数全解析）**；想了解 `pdftoppm` 渲染在流水线内部的具体用法，可以预习 [olmocr/data/renderpdf.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py)（第 2 单元 u2-l3 会精读它）。
