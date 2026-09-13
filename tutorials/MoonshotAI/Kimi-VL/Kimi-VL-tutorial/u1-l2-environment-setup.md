# 环境搭建与依赖解读：从 requirements.txt 到可运行环境

## 1. 本讲目标

学完本讲，你应该能够：

1. 按 README 的官方步骤，用 conda 创建 `python=3.10` 的 `kimi-vl` 独立环境，并完成依赖安装。
2. 逐行说清 `requirements.txt` 中 8 个依赖各自的用途，以及它们分别被 README 中哪段代码使用。
3. 理解 `==` 版本锁定与官方推荐组合（python=3.10 / torch=2.5.1 / transformers=4.51.3）背后的兼容性考量。
4. 说明 flash-attn 是什么、它为什么能省显存、在什么条件下值得安装、安装时 `--no-build-isolation` 参数的含义。

本讲不涉及模型推理本身——那是下一讲（u2-l1）的内容。本讲只解决一个问题：**把一个干净的环境变成「随时可以加载 Kimi-VL」的环境**。

## 2. 前置知识

上一讲（u1-l1）我们确认了两个关键事实，本讲会反复用到：

- 本仓库是**发布型仓库**，不含任何 Python 源码；模型实现与权重托管在 HuggingFace，通过 `trust_remote_code=True` 动态加载。
- Kimi-VL 总参数 16B、每 token 激活约 2.8B、支持 128K 上下文。

在动手之前，先补齐几个环境管理的基础概念：

- **虚拟环境（virtual environment）**：一个互相隔离的 Python 解释器 + 包集合。不同项目往往依赖同一个库的不同版本（A 项目要 torch 2.5，B 项目要 torch 2.7），不隔离就会互相踩踏。conda 和 Python 自带的 `venv` 都能做这件事，Kimi-VL 官方选择 conda。
- **pip 与 PyPI**：pip 是 Python 的包管理器，PyPI 是它默认的包仓库。`pip install -r requirements.txt` 表示「按这份清单逐行安装」。
- **依赖锁定**：`torch==2.5.1` 中的 `==` 表示精确安装这个版本；只写 `pillow` 不带版本号，则安装当时最新的稳定版。前者可复现性强，后者更省心但可能踩到不兼容。
- **CUDA / GPU**：CUDA 是 NVIDIA 的并行计算平台。深度学习推理默认在 GPU 上跑（通过 `torch` 的 CUDA 后端）；纯 CPU 也能加载模型，但速度慢得多。
- **显存（VRAM）**：GPU 上的内存。16B 参数的模型，权重本身就要吃掉几十 GB 显存，这也是后面讨论 flash-attn、bfloat16 的原因。

## 3. 本讲源码地图

本仓库虽然不含模型源码，但环境搭建相关的「源码证据」集中在两个文件里：

| 文件 | 作用 | 本讲关注的位置 |
| --- | --- | --- |
| `requirements.txt` | 全部 8 个依赖的清单，是这个仓库唯一的「工程配置文件」 | 全文 8 行，逐行精读 |
| `README.md` | Setup 步骤、推荐环境说明、flash-attn 提示与用法注释 | 第 6 节 Example usage 开头的 Setup 小节、以及调用 openai SDK 的代码段 |

另外，`figures/` 目录下的示例图片（`demo.png` 等）将在下一讲的推理实践中用到，本讲暂时只需要知道它们存在。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 conda 环境创建**、**4.2 依赖清单逐项解读**、**4.3 flash-attn 可选加速**。

### 4.1 conda 环境创建

#### 4.1.1 概念说明

环境隔离是一切工程的第一步。Kimi-VL 官方推荐的运行环境由三个组件的**特定版本组合**构成，README 在推理小节开头明确写了这句话：

> It is recommended to use python=3.10, torch=2.5.1, and transformers=4.51.3 as the development environment.

为什么组合如此重要？因为 Kimi-VL 通过 `trust_remote_code=True` 加载托管在 HuggingFace 上的模型实现代码，这些远程代码是针对特定版本的 transformers 内部接口编写的。torch、transformers、Python 三者中任何一个版本漂移，都可能出现「别人能跑我不能跑」的问题。conda 环境的价值就在于把这个组合封存在一个名叫 `kimi-vl` 的盒子里，与机器上其他项目互不干扰。

#### 4.1.2 核心流程

官方三步走的执行过程：

1. `conda create -n kimi-vl python=3.10 -y`：创建名为 `kimi-vl` 的环境，内置 Python 3.10 解释器；`-y` 表示对确认提问自动回答 yes。
2. `conda activate kimi-vl`：激活该环境，此后终端里的 `python` / `pip` 都指向这个盒子内部。
3. `pip install -r requirements.txt`：在盒子里按清单安装 8 个依赖（下一节逐行拆解）。

判断自己「是否已经在环境里」的方法：看终端提示符前缀是否出现 `(kimi-vl)`；或执行 `which python`（Linux/macOS）确认 python 路径落在 `envs/kimi-vl` 目录下。

#### 4.1.3 源码精读

Setup 步骤来自 README 第 6 节 Example usage 的开头：

[README.md:L98-L107](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L98-L107)

这段是环境搭建的「官方唯一入口」：第 101–103 行的三条命令分别完成创建、激活、装依赖；第 106–107 行的 Note 则给出 flash-attn 的可选安装提示（本讲 4.3 节展开）。

「python=3.10 从哪来」的出处是这一句：

[README.md:L109-L111](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L109-L111)

README 在介绍 Transformers 推理用法时声明：推荐以 python=3.10、torch=2.5.1、transformers=4.51.3 作为开发环境。注意这里的三个版本与 `requirements.txt` 中锁定的 torch/transformers 版本完全一致，互相印证。

#### 4.1.4 代码实践

1. **实践目标**：创建并进入官方推荐的隔离环境，确认 Python 版本正确。
2. **操作步骤**：
   ```bash
   git clone https://github.com/MoonshotAI/Kimi-VL.git
   cd Kimi-VL
   conda create -n kimi-vl python=3.10 -y
   conda activate kimi-vl
   python --version
   ```
3. **需要观察的现象**：执行 `conda activate` 后，终端提示符前缀出现 `(kimi-vl)`。
4. **预期结果**：`python --version` 输出 `Python 3.10.x`（x 为补丁号，具体数字随 conda 源缓存而异，待本地验证）。
5. 如需退出环境执行 `conda deactivate`；如需彻底删除重来，执行 `conda env remove -n kimi-vl`。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须指定 `python=3.10`，而不是让 conda 装最新版 Python？

**参考答案**：README L111 明确推荐 python=3.10 + torch=2.5.1 + transformers=4.51.3 的组合。Kimi-VL 的模型实现代码托管在 HuggingFace（`trust_remote_code` 加载），是针对这套版本组合开发和测试的；换用更新的 Python 可能遇到依赖包尚未发布对应轮子、或行为变化导致的不兼容。

**练习 2**：同事在同一台机器上有一个依赖 torch 2.7 的项目，你的 `kimi-vl` 环境会影响到他吗？

**参考答案**：不会。conda 环境之间、以及与系统 Python 之间相互隔离，各自维护独立的包目录；只要双方都在各自环境中操作（激活正确的环境），pip 安装互不干扰。这正是官方用 conda 而不是全局 pip 的原因。

### 4.2 依赖清单逐项解读

#### 4.2.1 概念说明

`requirements.txt` 是本仓库唯一的工程配置文件，全部内容只有 8 行。它回答了两个问题：

1. **跑通 README 中的示例最少需要装什么**——注意不是「跑通一切」：清单覆盖了第 6 节 Transformers 推理和第 8 节 OpenAI 客户端调用两条路径，但 **vLLM 部署并不在其中**（README L223 的 `from vllm import LLM` 需要你自行安装 vllm）。
2. **哪些版本是精确锁定的**：torch、torchvision、transformers 三个带 `==`，其余五个不带。锁定的三个恰好是 README L111 推荐组合中的两个库及其配套视觉库——与模型加载和远程代码执行强相关的部分被钉死，API 相对稳定的工具库则放开版本。

#### 4.2.2 核心流程

安装时 pip 的处理逻辑：

1. 逐行解析 `requirements.txt`，`==` 精确匹配版本，无版本号则解析为「最新兼容版」。
2. 递归解决每个依赖自己的依赖（例如 `transformers` 会带出 `tokenizers`、`safetensors` 等）。
3. 从 PyPI 下载并安装。torch 2.5.1 会根据机器情况选择 CPU 版或对应 CUDA 版的安装包。

装完之后，你可以用 `pip list` 查看环境里最终落地的完整包列表，用 `pip show torch` 查看单个包的详情。

#### 4.2.3 源码精读

清单全文如下：

[requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8)

逐行解读这张表（「README 中的证据」列给出你能在本仓库里亲眼看到的调用点）：

| 行 | 依赖 | 版本 | 作用 | README 中的使用证据 |
| --- | --- | --- | --- | --- |
| L1 | `torch` | ==2.5.1 | PyTorch 深度学习框架：张量运算、自动求导，`model.generate` 的底座 | L116 `import torch`，L146 `model.generate(...)` |
| L2 | `torchvision` | ==0.20.1 | PyTorch 官方视觉库，版本与 torch 严格配套（2.5.1 ↔ 0.20.1） | README 未直接 import；视觉预处理链路的配套基础设施 |
| L3 | `transformers` | ==4.51.3 | HuggingFace 模型库：`AutoModelForCausalLM` / `AutoProcessor`，`trust_remote_code` 的入口 | L118 导入，L121–L137 加载模型与 Processor |
| L4 | `pillow` | 不锁定 | 图像读取与基础处理（导入名叫 `PIL`） | L117 `from PIL import Image`，L139–L140 `Image.open` |
| L5 | `tiktoken` | 不锁定 | OpenAI 出品的高速 BPE 分词库，常用于模型分词环节 | 间接：具体调用点位于 HuggingFace 模型仓库的远程代码中，本仓库不可见（待确认） |
| L6 | `accelerate` | 不锁定 | 设备调度库：`device_map="auto"` 的自动切分/多卡分配依托它实现 | L124 `device_map="auto"` |
| L7 | `blobfile` | 不锁定 | 以统一接口读写本地 / Azure Blob / S3 文件的库 | 间接：为远端资源拉取兜底，具体调用点待确认 |
| L8 | `openai` | 不锁定 | OpenAI Python SDK，用来调用 vLLM 起的 OpenAI 兼容服务 | L272 `from openai import OpenAI` |

其中「间接使用」的三项（torchvision、tiktoken、blobfile）正体现了 u1-l1 的结论：**这个仓库看不到模型实现**，它们是被 HuggingFace 模型仓库中的远程代码或其传递依赖用到的，作者提前列进清单避免运行时报 `ModuleNotFoundError`。

两个值得记下的观察：

- **transformers 的调用入口**在 README 推理示例开头：[README.md:L116-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L116-L126)。`from_pretrained` 传入的 `torch_dtype="auto"`、`device_map="auto"`、`trust_remote_code=True` 三个参数分别决定了精度、设备分配和远程代码加载，下一讲会逐一展开。
- **openai 依赖的用武之地**在部署一节：[README.md:L268-L277](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L268-L277)。`OpenAI(base_url="http://localhost:8000/v1", ...)` 说明它不是去调 OpenAI 官方服务，而是调用本地 vLLM 起的兼容接口——SDK 是通用的，服务端是谁由 `base_url` 决定。

#### 4.2.4 代码实践

1. **实践目标**：安装全部依赖并验证版本与官方推荐一致。
2. **操作步骤**（确保已激活 `kimi-vl` 环境）：
   ```bash
   pip install -r requirements.txt
   python -c "import torch, transformers; print(torch.__version__, transformers.__version__)"
   ```
3. **需要观察的现象**：安装过程中 pip 会列出依赖树；torch 安装包体积较大（CUDA 版通常数 GB），耐心等待。
4. **预期结果**：第二条命令输出 `2.5.1 4.51.3`（待本地验证）。再执行 `pip list | grep -iE "torch|transformers|pillow|tiktoken|accelerate|blobfile|openai"`，应能看到 8 个包全部在列。
5. 如果你的机器有 NVIDIA GPU，可加一步 `python -c "import torch; print(torch.cuda.is_available())"` 确认 CUDA 可用；输出 `False` 通常意味着装到 CPU 版 torch 或驱动缺失，需先排查再继续下一讲。

#### 4.2.5 小练习与答案

**练习 1**：`torch==2.5.1` 换成 `torch>=2.5.1` 会发生什么？有什么风险？

**参考答案**：`>=` 会安装满足条件的最新版（比如 2.7.x）。风险在于：README L111 明确推荐并测试的是 2.5.1，模型远程代码、flash-attn 的编译、CUDA 扩展的 ABI 都是围绕该版本验证的；新版本可能改变内部行为或二进制接口，导致加载失败或结果异常。`==` 锁定牺牲一点新鲜度换取可复现性。

**练习 2**：README 第 8 节的 vLLM 示例运行时报 `ModuleNotFoundError: No module named 'vllm'`，为什么 `pip install -r requirements.txt` 没有装上它？

**参考答案**：`requirements.txt` 只覆盖 Transformers 推理与 OpenAI 客户端两条路径；vLLM 是独立的部署引擎，未列入清单（可对照 [requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8) 确认没有 vllm）。要走 vLLM 路线需要按 vLLM 官方文档另行安装，且 README L213 说明支持的是 vLLM 主分支版本。

**练习 3**：`device_map="auto"` 这个参数的背后是清单中的哪个依赖？去掉它会怎样？

**参考答案**：`accelerate`（清单 L6）。没有它，`from_pretrained(device_map="auto")` 会直接报错，因为 transformers 把设备映射与模型切分的实现委托给了 accelerate。

### 4.3 flash-attn 可选加速

#### 4.3.1 概念说明

flash-attn（FlashAttention）是一个**可选**的注意力加速库，README 用一条 Note 交代了它的定位：遇到显存不足（Out-of-Memory）或想加速推理时才需要装。也就是说，**不装它也能跑通官方示例**——README 的默认示例代码（L121–L126）没有用到它，真正启用它的是那段被注释掉的备选加载代码（L129–L135）。

它解决的痛点来自 Transformer 的注意力机制。标准实现的注意力公式为：

\[ \mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V \]

其中 \(QK^\top\) 是一个 \(n \times n\) 的打分矩阵（\(n\) 为序列长度）。标准做法会把这个矩阵**完整写进显存**再做 softmax，显存开销 \(O(n^2)\)。对 Kimi-VL 这种 128K 上下文的模型，\(n = 131072\)，单头单层的一次物化就要 \(n^2 \approx 1.7 \times 10^{10}\) 个元素，bf16 下约 32 GiB——这是简化估算，但足以说明问题：**上下文越长，平方级显存爆炸越致命**。

FlashAttention 的思路是把 Q、K、V 切成小块（tiling），在高速缓存（SRAM）里逐块计算，用「在线 softmax」边扫边维护每行的运行最大值与累加和，**从不物化完整的 \(n \times n\) 矩阵**，显存从 \(O(n^2)\) 降到 \(O(n)\)，同时减少了对慢速显存的读写次数，速度也随之提升。

#### 4.3.2 核心流程

flash-attn 接入推理链路的方式：

1. 安装：`pip install flash-attn --no-build-isolation`。
2. 修改模型加载参数：`attn_implementation="flash_attention_2"`，并把 `torch_dtype` 显式设为 `torch.bfloat16`。
3. transformers 在加载时检测 flash-attn 是否可用：未安装却传了该参数会直接报错；安装成功则把注意力层路由到 flash-attn 内核。
4. 收益：注意力显存从平方级降为线性级，权重用 bf16 存储再省一半——两者叠加，才撑得起长上下文与更大批量的输入。

关于 bf16 的账：bfloat16 每个参数占 2 字节，fp32 占 4 字节。16B 参数的模型仅权重一项，就从约 64 GB 降到约 32 GB。这正是 README 注释里「save memory and speed up inference」的具体含义。

使用条件：需要 NVIDIA GPU 且架构受 flash-attn 支持（建议 Ampere 及更新架构，具体支持列表以 flash-attn 官方文档为准）；flash-attn 2 要求半精度（fp16/bf16）输入，所以它和 `torch_dtype=torch.bfloat16` 总是成对出现；纯 CPU 环境用不了，跳过即可。

#### 4.3.3 源码精读

安装提示来自 Setup 小节的 Note：

[README.md:L106-L107](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L106-L107)

原文说得很清楚：遇到 OOM 或想加速时，用 `pip install flash-attn --no-build-isolation` 安装。`--no-build-isolation` 的含义：flash-attn 需要在安装时编译 CUDA/C++ 扩展，pip 默认的「构建隔离」会另起一个干净的构建环境，里面没有你刚装好的 torch，编译时就会因找不到匹配的 torch 头文件而失败；加上这个参数让编译直接复用当前环境里的 torch，前提是**先装好 requirements.txt、再装 flash-attn**，顺序不能反。

启用方式在推理示例的注释块中：

[README.md:L127-L135](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L127-L135)

对比默认加载（L121–L126 的 `torch_dtype="auto"`），这段被注释的备选写法改了两处：`torch_dtype=torch.bfloat16`（权重减半）和新增 `attn_implementation="flash_attention_2"`（注意力走 flash-attn 内核）。把这两行替换进默认代码，就是官方推荐的「省显存 + 提速」配置。

#### 4.3.4 代码实践

1. **实践目标**：在 GPU 环境安装 flash-attn 并验证可导入。
2. **操作步骤**（确认 `python -c "import torch; print(torch.cuda.is_available())"` 输出 True 后再执行）：
   ```bash
   pip install flash-attn --no-build-isolation
   python -c "import flash_attn; print(flash_attn.__version__)"
   ```
3. **需要观察的现象**：若没有匹配的预编译轮子，pip 会触发源码编译，可能耗时十几分钟到更久，属于正常现象。
4. **预期结果**：第二条命令打印出版本号即安装成功（能否命中预编译轮子取决于 GPU 架构与 CUDA 版本，待本地验证）。纯 CPU 环境请跳过安装，仅记录 `--no-build-isolation` 的含义即可。
5. 更进一步：把 README L129–L135 的注释代码替换进默认示例，观察加载日志中的注意力实现是否变为 flash attention 2——完整推理对比留给下一讲。

#### 4.3.5 小练习与答案

**练习 1**：用一句话向同事解释 flash-attn 为什么省显存。

**参考答案**：它把注意力计算分块后在高速缓存中流式完成，从不把 \(n \times n\) 的完整注意力矩阵写进显存，显存从 \(O(n^2)\) 降为 \(O(n)\)——对 128K 上下文的 Kimi-VL 尤其关键。

**练习 2**：为什么安装命令必须带 `--no-build-isolation`，且必须装在 torch 之后？

**参考答案**：flash-attn 安装时要编译 CUDA 扩展，编译配置需要读取当前环境的 torch 版本与头文件；默认构建隔离会使用不含 torch 的干净环境导致编译失败。所以先 `pip install -r requirements.txt` 装好 torch 2.5.1，再装 flash-attn。

**练习 3**：README 的 flash-attn 推荐配置为什么同时要求 `torch_dtype=torch.bfloat16`，而不是继续用 `torch_dtype="auto"`？

**参考答案**：两个原因。其一，flash-attn 2 内核要求半精度（bf16/fp16）输入；其二，显式 bf16 让 16B 权重的存储从约 64 GB 降到约 32 GB，与「省显存」的目标一致。`"auto"` 跟随模型配置文件中的精度，不如显式声明来得确定。

## 5. 综合实践

把三个模块串起来，写一个**环境体检脚本**，一次性验证本讲搭好的环境。以下是示例代码（非仓库原有文件，可保存为 `check_env.py` 放在仓库外任意目录）：

```python
# 示例代码：check_env.py —— 逐项检查 Kimi-VL 运行环境
import importlib

# (导入名, 期望版本或 None)  注意 pillow 的导入名是 PIL
DEPS = [
    ("torch", "2.5.1"),
    ("torchvision", "0.20.1"),
    ("transformers", "4.51.3"),
    ("PIL", None),
    ("tiktoken", None),
    ("accelerate", None),
    ("blobfile", None),
    ("openai", None),
]

for mod_name, expected in DEPS:
    try:
        mod = importlib.import_module(mod_name)
        version = getattr(mod, "__version__", "未知")
        flag = "" if expected in (None, version) else f"  <-- 期望 {expected}"
        print(f"{mod_name:<12} {version:<10}{flag}")
    except ImportError:
        print(f"{mod_name:<12} {'未安装':<8}  <-- 缺失")

import torch
print("CUDA 可用:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU 型号:", torch.cuda.get_device_name(0))
    try:
        import flash_attn
        print("flash-attn:", flash_attn.__version__)
    except ImportError:
        print("flash-attn: 未安装（可选，GPU 用户建议安装）")
```

执行 `python check_env.py`，验收标准：

1. `requirements.txt` 清单中的 8 个包全部打印出版本、无一缺失；
2. torch 显示 `2.5.1`、torchvision 显示 `0.20.1`、transformers 显示 `4.51.3`，与 README L111 推荐组合一致；
3. GPU 机器上 `CUDA 可用: True`，flash-attn 显示版本号或明确的「未安装」提示。

运行结果与显存占用对比属于待本地验证项：如果你有 GPU，建议顺手记下 `nvidia-smi` 中的空闲显存，下一讲加载模型时对照。

## 6. 本讲小结

- 官方环境三件套是 **conda 环境 `kimi-vl`（python=3.10）+ `pip install -r requirements.txt` + 可选 flash-attn**，出处为 [README.md:L98-L107](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L98-L107)。
- `requirements.txt` 只有 8 行：torch/torchvision/transformers 用 `==` 精确锁定（对应官方推荐组合），pillow/tiktoken/accelerate/blobfile/openai 不锁版本；其中 tiktoken、blobfile、torchvision 主要服务于 HuggingFace 上的远程模型代码。
- 清单**不含 vllm**——Transformers 推理与 OpenAI 客户端路径由清单覆盖，vLLM 部署需另行安装。
- flash-attn 是可选项：通过分块计算避免物化 \(n \times n\) 注意力矩阵，显存 \(O(n^2) \to O(n)\)，配合 `torch_dtype=torch.bfloat16`（16B 权重约 64 GB → 32 GB）构成官方推荐的省显存配置；安装须在 torch 之后并加 `--no-build-isolation`。

## 7. 下一步学习建议

环境就绪后，下一讲 **u2-l1「第一次推理：用 Transformers 跑通 Kimi-VL-A3B-Instruct 单图问答」** 将逐行拆解 README 推理示例：`AutoModelForCausalLM.from_pretrained` 的四个参数、messages 结构、`apply_chat_template`、`processor` 编码与输出裁剪解码，并用 `figures/demo.png` 完成第一次真实推理。建议提前通读 [README.md:L113-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L113-L154) 的 Instruct 示例代码，带着「每一行为什么这么写」的问题进入下一讲。
