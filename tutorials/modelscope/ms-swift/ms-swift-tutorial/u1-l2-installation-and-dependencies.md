# 安装与环境依赖

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `pip`、`uv` 和「源码可编辑安装」三种方式把 ms-swift 装到本地，并验证安装是否成功。
- 看懂 `setup.py` 是如何把一份 `requirements.txt` 解析成运行依赖、又如何注册出 `swift` 与 `megatron` 两个命令行入口的。
- 理解「可选依赖分组」机制：`ms-swift[megatron]`、`ms-swift[eval]`、`ms-swift[ray]`、`ms-swift[all]` 这些方括号到底装了什么、对应 `requirements/` 目录里的哪些文件。
- 对照官方版本矩阵，判断自己机器上的 Python / torch / transformers / vllm 版本是否满足要求。

本讲是「动手」的第一步：上一讲（u1-l1）我们已经从 README 建立了对 ms-swift 是什么的全局认知，本讲不再重复能力矩阵，而是聚焦在「怎么把它装起来、装的时候背后发生了什么」。

## 2. 前置知识

在开始之前，你需要了解几个基础概念。如果已经熟悉可以跳过。

- **Python 包与 `setup.py`**：一个 Python 项目通过 `setup.py`（或 `pyproject.toml`）向系统声明「我叫什么名字、依赖哪些库、提供哪些命令」。`pip install` 本质上就是读取这份声明、把代码和元信息装进你的环境。
- **`requirements.txt`**：一份纯文本的依赖清单，每行一个包名（可带版本约束）。`pip install -r requirements.txt` 会照单安装。
- **`entry_points` / `console_scripts`**：这是「装完之后能在终端敲的命令」的注册表。比如声明了 `swift=swift.cli.main:cli_main`，装好后终端里输入 `swift` 就等价于「调用 `swift.cli.main` 这个模块里的 `cli_main` 函数」。
- **可选依赖（extras）**：写在 `pip install 'ms-swift[all]'` 方括号里的标签。它让你「按需安装」——只想做 SFT 就装最小集合，想用 Megatron 并行才额外装那一组重量级依赖。
- **`uv`**：一个用 Rust 写的、比 `pip` 快得多的包管理器，用法和 `pip` 几乎一致（`uv pip install ...`）。ms-swift 官方文档把它作为可选的加速方案。

> 术语提示：本讲会反复出现 `transformers`、`peft`、`trl`、`vllm`、`deepspeed` 这些库。它们是 ms-swift 的「地基」——ms-swift 并不重新实现训练/推理算法，而是把这些库粘合在一起，用统一的 `swift` 命令暴露出来。理解这一点，就能理解为什么 `requirements/` 里有那么多版本约束。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [setup.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py) | 项目的安装清单：声明包名、版本、依赖、可选依赖分组、命令行入口。本讲的核心。 |
| [requirements.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements.txt) | 顶层依赖清单，本身只有一行，指向真正的内容文件。 |
| [requirements/framework.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt) | 框架核心依赖（transformers/peft/trl/datasets 等），是「最小可用集合」。 |
| [requirements/megatron.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/megatron.txt) | `[megatron]` 分组依赖（megatron-core / mcore-bridge / peft）。 |
| [requirements/eval.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/eval.txt) | `[eval]` 分组依赖（evalscope 及其评测后端）。 |
| [requirements/ray.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/ray.txt) | `[ray]` 分组依赖。 |
| [requirements/swanlab.txt](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/swanlab.txt) | `[swanlab]` 分组依赖（训练日志可视化）。 |
| [requirements/install_all.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/install_all.sh) | 「全能力」参考脚本：按顺序把推理、训练、多模态、Megatron、apex 等所有可选件都装上。 |
| [swift/cli/main.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py) | `swift` 命令的真正入口函数 `cli_main`，本讲用它解释「为什么验证安装要用 `swift sft --help`」。 |
| [docs/source/GetStarted/SWIFT-installation.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source/GetStarted/SWIFT-installation.md) | 官方安装文档，含 wheel / 源码 / 镜像 / 硬件 / 版本矩阵。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看 `setup.py` 怎么解析依赖和注册命令，再看 `requirements/` 目录怎么把依赖分组，最后看官方的版本矩阵告诉我们运行环境要求。

### 4.1 setup.py 的依赖解析与 entry_points

#### 4.1.1 概念说明

当你执行 `pip install ms-swift` 时，pip 会去仓库里找 `setup.py`（或 `pyproject.toml`）并读取其中的元信息。ms-swift 的 `setup.py` 干了三件与本讲直接相关的事：

1. **解析依赖**：通过一个自定义函数 `parse_requirements`，把 `requirements.txt`（以及它 `-r` 引用的其他文件）读成一个 Python 列表，作为 `install_requires`。
2. **声明可选依赖分组**：把 `requirements/` 下的若干文件分别读成 `megatron`、`eval`、`swanlab`、`ray` 等分组，再拼出一个 `all` 组合，整体作为 `extras_require`。
3. **注册命令行入口**：通过 `entry_points={'console_scripts': ...}` 注册 `swift` 和 `megatron` 两个终端命令。

理解这三点后，你就能回答「为什么装完 ms-swift 之后终端会出现一个叫 `swift` 的命令」——它不是魔法，而是 `console_scripts` 注册表写进去的。

#### 4.1.2 核心流程

`setup.py` 主流程（`if __name__ == '__main__'` 之后）的伪代码如下：

```
1. install_requires = parse_requirements('requirements.txt')   # 解析核心依赖
2. extra_requires = {}
3. 对每个分组名 in [megatron, eval, swanlab, ray]:
       extra_requires[分组名] = parse_requirements(f'requirements/{分组名}.txt')
4. all_requires = install_requires + eval + swanlab + ray        # 注意：all 不含 megatron
   extra_requires['all'] = all_requires
5. setup(
       install_requires=install_requires,
       extras_require=extra_requires,
       entry_points={'console_scripts': [
           'swift=swift.cli.main:cli_main',
           'megatron=swift.cli._megatron.main:cli_main',
       ]},
       python_requires='>=3.8.0',
       ...
   )
```

`parse_requirements` 内部对每一行做解析：遇到 `-r 其他文件` 就递归展开（这就是 `requirements.txt` 里 `-r requirements/framework.txt` 生效的原因）；遇到 `包名>=1.0` 这种带版本约束的行，就拆成「包名 + 运算符 + 版本」。空行、注释行（`#` 开头）和 `--` 开头的指令行会被跳过。

#### 4.1.3 源码精读

先看依赖解析的入口与 `setup()` 调用。`parse_requirements` 是一个自定义函数，负责把文本清单变成结构化的依赖列表：

- [setup.py:L24-L116](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L24-L116)：`parse_requirements` 函数定义。注意 L48-L54 处理 `-r ` 前缀的行——它会递归读取被引用的文件，这正是 `requirements.txt` 一行 `-r requirements/framework.txt` 能把整个核心依赖拉进来的关键。

主程序里，先解析核心依赖，再逐个解析可选分组：

- [setup.py:L120-L131](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L120-L131)：核心依赖 `install_requires` 来自 `requirements.txt`；`extra_requires` 的 `megatron/eval/swanlab/ray` 四组分别来自对应文件。注意 L127-L131 的 `all_requires` 只 `extend` 了 `install_requires + eval + swanlab + ray`，**并没有包含 megatron**——也就是说 `ms-swift[all]` 不会自动装 Megatron 那套（megatron-core 等），需要单独 `ms-swift[megatron]` 或参照 `install_all.sh` 手动装。这是一个容易踩坑的点。

接着看版本与入口声明：

- [setup.py:L146-L157](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L146-L157)：`python_requires='>=3.8.0'` 声明最低 Python 版本；`classifiers` 列出兼容的 Python 版本范围（3.8–3.12）。注意：这里的 `>=3.8.0` 是历史遗留的宽松声明，而**官方文档推荐 Python >=3.10、推荐 3.12**（见 4.3 节矩阵），二者不矛盾——`python_requires` 是「能装的最低门槛」，文档推荐的是「实际跑得稳的版本」。

- [setup.py:L162-L164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L162-L164)：`entry_points` 注册了两个命令：`swift` 指向 `swift.cli.main:cli_main`，`megatron` 指向 `swift.cli._megatron.main:cli_main`。装完后，pip 会在你的环境 `bin/` 目录生成同名可执行脚本。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `entry_points` 注册出的命令脚本，把抽象的「console_scripts」变成可触摸的文件。

**操作步骤**（源码可编辑安装）：

1. 进入仓库根目录（`setup.py` 所在目录）：
   ```bash
   cd /path/to/ms-swift
   ```
2. 以可编辑模式安装（`-e` 表示「软链」，之后改源码立即生效，适合学习/开发）：
   ```bash
   pip install -e .
   # 或用 uv 加速：
   # uv pip install -e . --torch-backend=auto
   ```
3. 查看安装信息和入口脚本位置：
   ```bash
   pip show ms-swift          # 看 Location（装在哪）、Version
   which swift                # 看 swift 命令脚本落在哪（通常是 <env>/bin/swift）
   which megatron             # 同上，对应 megatron 命令
   ```
4. 打开 `which swift` 指向的那个文件，你会看到一个 Python 包装脚本，其 `entry_points` 一行写着 `swift.cli.main:cli_main`——和 `setup.py` L162-L164 完全对应。

**需要观察的现象**：

- `pip show ms-swift` 的 Version 与 [swift/version.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/version.py) 里的 `__version__`（当前为 `4.5.0.dev0`）一致。
- `which swift` 能打印出一条绝对路径，说明 `console_scripts` 注册成功。

**预期结果**：安装无报错，`which swift` / `which megatron` 都有输出。

> 待本地验证：如果你在最小依赖环境下装，`pip install -e .` 只会拉 `framework.txt` 里的核心依赖；若缺 GPU 版 torch，`vllm`/`flash-attn` 等需要另外按 `install_all.sh` 装。把遇到的报错记下来，是本讲最重要的收获。

#### 4.1.5 小练习与答案

**练习 1**：`setup.py` 里 `entry_points` 注册了两个命令，分别是什么、各自指向哪个函数？

> **答案**：`swift` → `swift.cli.main:cli_main`；`megatron` → `swift.cli._megatron.main:cli_main`。两个命令复用了同一个入口函数名 `cli_main`，只是模块不同。

**练习 2**：为什么 `pip install -e .` 之后改了源码不用重装？

> **答案**：`-e`（editable）会把一个指向源码目录的「软链」放进 site-packages，import 时直接读你工作区里的源文件，所以改完即时生效。普通 `pip install .` 会把代码拷贝一份进 site-packages，改源码不会影响已安装的包。

---

### 4.2 requirements 目录的分组依赖

#### 4.2.1 概念说明

ms-swift 的能力横跨训练、推理、评测、量化、部署、Megatron 大规模并行、Ray 分布式等，每一块背后的依赖库都不同（且有的很重、有的还要编译）。如果把这些全部塞进一个 `install_requires`，用户只想做个 LoRA 微调也得下载几十 GB——体验很差。

所以项目采用了「**核心最小 + 可选分组**」的策略：

- **核心依赖**：放进 `requirements/framework.txt`，装 ms-swift 默认就装它，保证「能跑 SFT/infer」。
- **可选分组**：放进 `requirements/` 下的独立文件，通过 `pip install 'ms-swift[分组名]'` 按需追加。

这与 `setup.py` L121-L131 的 `extras_require` 一一对应。此外，仓库还提供了 `requirements/install_all.sh`——一份「全能力」参考脚本，把所有能装的（含 vllm、deepspeed、flash-attn、多模态工具库、Megatron、apex）按推荐顺序装一遍。

#### 4.2.2 核心流程

依赖分组的映射关系如下表（来源：`setup.py` L123-L131 + `requirements/` 各文件）：

| extras 标签 | 对应文件 | 主要内容 | 装它的命令 |
| --- | --- | --- | --- |
| （核心，无标签） | `requirements/framework.txt` | transformers / peft / trl / datasets / modelscope / gradio 等约 40 个核心库 | `pip install ms-swift` |
| `[megatron]` | `requirements/megatron.txt` | megatron-core、mcore-bridge、peft | `pip install 'ms-swift[megatron]'` |
| `[eval]` | `requirements/eval.txt` | evalscope 及 opencompass/vlmeval 后端 | `pip install 'ms-swift[eval]'` |
| `[ray]` | `requirements/ray.txt` | ray | `pip install 'ms-swift[ray]'` |
| `[swanlab]` | `requirements/swanlab.txt` | swanlab（训练看板） | `pip install 'ms-swift[swanlab]'` |
| `[all]` | （`setup.py` 动态拼接） | 核心 + eval + swanlab + ray，**不含 megatron** | `pip install 'ms-swift[all]'` |

> 重要提醒：`[all]` **不包含 Megatron**（见 4.1.3 对 setup.py L127-L131 的分析）。要用 Megatron-SWIFT 做大规模并行训练，要么单独 `pip install 'ms-swift[megatron]'`，要么直接参照 `install_all.sh` 手动装那一套（因为 megatron-core / TransformerEngine / apex 等大多需要源码编译，用 extras 自动装未必稳）。

#### 4.2.3 源码精读

先看核心依赖 `framework.txt`，它是「最小可用集合」，几乎所有的版本约束都集中在这里：

- [requirements/framework.txt:L1-L39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt#L1-L39)：核心依赖清单。几条关键约束——
  - L9 `datasets>=3.0,<4.8.5`：datasets 上限是 4.8.5。
  - L22 `peft>=0.11,<0.20`：peft 版本窗口。
  - L35 `transformers>=4.33,<5.13.0`：transformers 必须低于 5.13。
  - L37 `trl>=0.15,<1.0`：trl（RLHF 训练器）版本窗口。

  注意：`torch` 并不在这个文件里——torch 由用户自己按 CUDA 版本安装（或用 `uv ... --torch-backend=auto` 自动选）。

再看各可选分组，它们都很短：

- [requirements/megatron.txt:L1-L3](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/megatron.txt#L1-L3)：`mcore-bridge>=1.4.0`、`megatron-core>=0.15`、`peft>=0.15`。这三行就是 `pip install 'ms-swift[megatron]'` 实际装的东西（megatron-core 通常要编译，耗时较长）。
- [requirements/eval.txt:L1-L4](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/eval.txt#L1-L4)：`evalscope>=1.0.0` 及其 `[opencompass]`、`[vlmeval]` 两个评测后端 extras。
- [requirements/ray.txt:L1-L1](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/ray.txt#L1-L1) 与 [requirements/swanlab.txt:L1-L1](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/swanlab.txt#L1-L1)：各只有一行。

最后看「全能力」参考脚本 `install_all.sh`，它体现了真实的安装顺序与额外依赖（很多是 `requirements/` 文件里没有的，因为需要特殊编译）：

- [requirements/install_all.sh:L1-L20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/install_all.sh#L1-L20)：逐行解读要点——
  - L1 注释明确推荐环境：`python=3.10/3.11, cuda12.*`（注意脚本注释写 3.10/3.11，而 README 矩阵推荐 3.12，二者都属于「可用范围」，以你本地 CUDA 实际情况为准）。
  - L4-L6：先装推理栈 `vllm`，再钉住 `transformers/trl/peft/datasets` 版本，再装 `optimum/bitsandbytes/gradio/mcore-bridge`。
  - L7：直接从 git 安装全能力 ms-swift：`pip install "ms-swift[all]@git+..."`。
  - L8-L11：训练与多模态周边：`deepspeed<0.19`、`ray`、`timm`、多模态工具库（`qwen_vl_utils` 等）、音视频库（`decord/librosa`）。
  - L12：`flash-attn==2.8.3` 带 `--no-build-isolation`（flash-attn 需要复用已装好的 torch 来编译，所以关掉构建隔离）。
  - L13-L19：Megatron 相关的源码编译件（TransformerEngine、DeepGEMM、causal-conv1d 等），这些都 `--no-build-isolation`。
  - L20：注释 `# apex`，提示还需另装 NVIDIA apex。

#### 4.2.4 代码实践

**实践目标**：直观对比「最小安装」与「全能力安装」在依赖体积上的差异，建立「按需安装」的直觉。

**操作步骤**（源码阅读型 + 可选实操）：

1. **纯阅读型（无需装环境）**：打开 `requirements/install_all.sh`，把每一行 `pip install` 安装的包名摘出来，按「推理 / 训练 / 多模态 / Megatron」四类分组列成一张表。你会看到 `[all]` 之外的依赖比 `[all]` 本身多得多——这正是脚本存在的意义。
2. **实操型（若有 GPU 环境）**：在两个干净环境里分别装，对比 `pip list`：
   ```bash
   # 环境 A：最小
   pip install ms-swift
   pip list | wc -l

   # 环境 B：全能力（不含 megatron）
   pip install 'ms-swift[all]'
   pip list | wc -l
   ```

**需要观察的现象**：环境 B 的包数量明显多于环境 A；多出来的主要是 evalscope、ray、swanlab 及其传递依赖。

**预期结果**：`ms-swift[all]` 比 `ms-swift` 多装 eval/ray/swanlab 三组。Megatron 那套即使 `[all]` 也不会自动装。

> 待本地验证：具体多出多少个包，取决于你的基线环境，不必追求精确数字，重点感受「分组」带来的按需节省。

#### 4.2.5 小练习与答案

**练习 1**：`pip install 'ms-swift[all]'` 会自动装上 megatron-core 吗？为什么？

> **答案**：不会。`setup.py` L127-L131 的 `all_requires` 只拼接了 `install_requires + eval + swanlab + ray`，没有 `extend` megatron 分组。要装 Megatron 需单独 `pip install 'ms-swift[megatron]'` 或参照 `install_all.sh`。

**练习 2**：`requirements.txt` 只有一行 `-r requirements/framework.txt`，这行是怎么生效的？

> **答案**：`setup.py` 的 `parse_requirements` 在 L48-L54 识别到 `-r ` 前缀，会递归读取被引用的文件（`requirements/framework.txt`），把后者里的每一行当作依赖解析进来。所以 `requirements.txt` 实际等于 `framework.txt` 的内容。

---

### 4.3 运行环境版本矩阵

#### 4.3.1 概念说明

光把 ms-swift 装上还不够，还得保证「地基库」之间版本兼容。深度学习栈的版本耦合很紧：torch 的版本要匹配 CUDA；transformers 的版本要匹配 peft/trl/datasets；vllm 又对 torch/transformers 有自己的要求。装错一个就可能「能 import 但跑起来报奇怪的错」。

官方在安装文档 [SWIFT-installation.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source/GetStarted/SWIFT-installation.md) 里给出了一张「运行环境矩阵」和一张「硬件矩阵」，这是排错时的第一手依据。

#### 4.3.2 核心流程

判断环境是否可用的思路：

```
1. 看 Python：必须 >=3.10（文档推荐），setup.py 的 python_requires 写的是更宽松的 >=3.8。
2. 看 CUDA：有 N 卡才需要，推荐 cuda12.8/13.0；CPU/NPU/MPS 不需要 CUDA。
3. 看 torch：>=2.0，推荐 2.8.0/2.11.0。torch 必须和 CUDA 对齐。
4. 看 transformers：>=4.33 且 <5.13（framework.txt L35 的硬约束）。
5. 看用途：
   - 想做 RLHF：装 trl (>=0.15,<1.0)。
   - 想做训练加速：装 deepspeed (>=0.14)、flash-attn。
   - 想做推理/部署：装 vllm (>=0.5.1) 或 sglang (>=0.4.6)。
   - 想做评测：装 evalscope (>=1.0)，即 [eval] 分组。
   - 想用 Web-UI/App：gradio（推荐 5.32.1）。
```

硬件支持矩阵（来自安装文档「支持的硬件」一节）：A10/A100/H100、RTX 20/30/40 系列原生支持；T4/V100、Ascend NPU 部分模型可能出现 NAN 或算子不支持；MPS、CPU 也支持但有限制。

#### 4.3.3 源码精读

版本约束散落在三处，互相印证：

- **`setup.py` 的宽松下限**：[setup.py:L146](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L146) `python_requires='>=3.8.0'`，以及 L147-L157 的 classifiers（声明兼容 Python 3.8–3.12）。这是「能装」的最低门槛。
- **`framework.txt` 的硬约束**：[requirements/framework.txt:L9](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt#L9)（datasets `<4.8.5`）、[L22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt#L22)（peft `<0.20`）、[L35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt#L35)（transformers `<5.13.0`）、[L37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/framework.txt#L37)（trl `<1.0`）。这些是 pip 解析依赖时会强制执行的版本窗。
- **`install_all.sh` 的钉版本**：[requirements/install_all.sh:L4-L6](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/requirements/install_all.sh#L4-L6) 用 `pip install "vllm>=0.5.1"`、`"transformers<5.13" "trl<1.0" "peft<0.20" "datasets<4.8.5"` 把关键版本钉死，与 `framework.txt` 完全一致。

- **官方文档矩阵**：[docs/source/GetStarted/SWIFT-installation.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source/GetStarted/SWIFT-installation.md) 的「运行环境」表给出「范围 / 推荐 / 备注」三列，例如 python 推荐 3.12、transformers 推荐 4.57.6/5.12.1、vllm 推荐 0.11.0/0.23.0、deepspeed 推荐 0.18.9。当文档推荐值与 `framework.txt` 约束冲突时，以 `framework.txt` 的硬约束为准（因为它会被 pip 强制执行）。

把这三处对照看，就能快速判断「我这个环境能不能跑」。

#### 4.3.4 代码实践

**实践目标**：验证安装是否真正可用，并理解「为什么验证要用 `swift sft --help` 而不是 `swift --help`」。

**操作步骤**：

1. 检查关键库版本是否落在约束窗内：
   ```bash
   python -c "import torch, transformers, peft, datasets; \
print('torch', torch.__version__); \
print('transformers', transformers.__version__); \
print('peft', peft.__version__); \
print('datasets', datasets.__version__)"
   ```
   对照 `framework.txt`：transformers 应 `<5.13`、peft 应 `<0.20` 且 `>=0.11`、datasets 应 `<4.8.5` 且 `>=3.0`。
2. 验证 `swift` 命令可用——**注意用子命令形式**：
   ```bash
   swift sft --help     # 正确：sft 是已注册的子命令，会进入 argparse 打印帮助
   ```
3. （可选）确认 `swift --help` 的行为：根据对 `cli_main` 的源码阅读（见下方说明），它**不会**打印帮助。

**需要观察的现象与源码解释**：

为什么 `swift sft --help` 能用、而 `swift --help` 不行？答案在入口分发函数里。看 [swift/cli/main.py:L86-L102](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L86-L102)：`cli_main` 把命令行第一个参数当作 `method_name`（L89），再用它去 `ROUTE_MAPPING` 里查（L91）。`ROUTE_MAPPING` 的合法键是 `pt/sft/infer/...` 等子命令（见 [swift/cli/main.py:L14-L27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L14-L27)），并不包含 `--help`。所以 `swift --help` 会让 `route_mapping['--help']` 抛 `KeyError`。

而 `swift sft --help` 走的路径是：`cli_main` 把 `sft` 路由到 [swift/cli/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py)（该文件在 `__main__` 里调用 `sft_main`），`--help` 透传给后续基于 argparse/HfArgumentParser 的参数解析，于是正常打印 SFT 的全部参数帮助。这条调用链本身是 u1-l4（CLI 入口与命令分发）的主题，这里只需记住结论：**验证 ms-swift 是否装好，用 `swift sft --help` 这种「子命令 + --help」形式最可靠。**

**预期结果**：`swift sft --help` 打印出一长串 SFT 参数（model、dataset、lora 等），说明安装与参数体系都正常。

> 待本地验证：`swift --help` 是否真的抛 `KeyError`，可自行在装好 ms-swift 的环境里试一次确认；无论结果如何，验证安装都应优先用 `swift sft --help`。

#### 4.3.5 小练习与答案

**练习 1**：你的同事报告「装完 ms-swift 后 import 报错，transformers 版本 5.15」。问题出在哪？

> **答案**：`framework.txt` L35 要求 `transformers>=4.33,<5.13.0`，5.15 超出上限。需要降级 transformers 到 `<5.13`（文档推荐 4.57.6 或 5.12.1）。

**练习 2**：`setup.py` 写 `python_requires='>=3.8.0'`，但文档推荐 Python 3.12，两者矛盾吗？

> **答案**：不矛盾。`python_requires` 是「pip 允许安装的最低门槛」（历史遗留的宽松声明），文档推荐值是「实际跑得最稳的版本」。装得上不等于跑得好，生产环境应按文档推荐选 Python 3.10+/3.12。

**练习 3**：想用 ms-swift 做模型评测，最少要怎么装？

> **答案**：`pip install 'ms-swift[eval]'`，它会拉 `evalscope>=1.0.0` 及 opencompass/vlmeval 后端（见 `requirements/eval.txt`）。配合核心依赖即可使用 `swift eval`。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从零到可用」的源码安装与验证。

**任务**：在本地用源码可编辑方式安装 ms-swift，确认命令入口与关键库版本，并把过程中遇到的依赖问题记录成一份排错清单。

**步骤**：

1. **准备环境**：确认 Python（推荐 3.10–3.12）、CUDA（N 卡用户）。建议建一个独立虚拟环境：
   ```bash
   python -m venv swift-env && source swift-env/bin/activate
   ```
2. **克隆并安装**：
   ```bash
   git clone https://github.com/modelscope/ms-swift.git
   cd ms-swift
   pip install -e .            # 或 uv pip install -e . --torch-backend=auto
   ```
3. **核对版本**：运行 4.3.4 的 `python -c "..."` 一行命令，把 torch/transformers/peft/datasets 版本抄下来，逐项对照 `framework.txt` 的约束窗，标出任何越界的库。
4. **验证入口**：
   ```bash
   pip show ms-swift           # 记录 Version 与 Location
   which swift                 # 记录路径，并打开该文件确认 entry_points
   swift sft --help            # 能打印 SFT 参数即安装成功
   ```
5. **按需扩展**：根据你想做的事追加 extras——
   - 想评测：`pip install -e '.[eval]'`
   - 想全能力（不含 megatron）：`pip install -e '.[all]'`
   - 想 Megatron 并行：参照 `requirements/install_all.sh` 的 L13-L19 手动装（megatron-core 通常要编译）。
6. **记录问题**：把安装过程中遇到的每一个报错（缺 CUDA、flash-attn 编译失败、版本冲突等）记下，并写下你是怎么解决的。这份清单就是你下次重建环境时的最佳参考。

**验收标准**：`swift sft --help` 正常输出帮助；四个核心库版本全部落在约束窗内；至少记录 1 个真实遇到的依赖问题及解法（若全程顺利，也如实写「环境匹配，无报错」）。

> 待本地验证：是否需要额外装 deepspeed/vllm/flash-attn，取决于你要跑的模型与是否多卡。最小验证只需核心依赖 + `swift sft --help` 通过。

---

## 6. 本讲小结

- ms-swift 的安装清单在 `setup.py`：它通过自定义的 `parse_requirements` 把 `requirements.txt`（→ `framework.txt`）解析成核心依赖，并支持 `-r` 递归引用。
- 可选依赖用 `extras_require` 分组，对应 `requirements/` 下的 `megatron.txt / eval.txt / ray.txt / swanlab.txt`；`[all]` = 核心 + eval + swanlab + ray，**不含 megatron**。
- 两个命令 `swift` 和 `megatron` 由 `entry_points` 的 `console_scripts` 注册，分别指向 `swift.cli.main:cli_main` 和 `swift.cli._megatron.main:cli_main`。
- `requirements/install_all.sh` 是「全能力」参考脚本，额外覆盖 vllm/deepspeed/flash-attn/多模态库/Megatron/apex，其中需要编译的件都用 `--no-build-isolation`。
- 版本约束三处印证：`setup.py` 的宽松下限、`framework.txt` 的硬窗、文档矩阵的推荐值；冲突时以 `framework.txt` 硬约束为准。
- 验证安装要用 `swift sft --help`（子命令路由 + argparse），而非 `swift --help`——因为 `cli_main` 把第一个参数当 `ROUTE_MAPPING` 的键查，`--help` 不是合法键。

## 7. 下一步学习建议

装好之后，建议按顺序往下走：

1. **u1-l3 目录结构与模块化架构**：进入 `swift/` 包内部，理解一级模块（arguments/template/model/dataset/trainers/...）的职责划分与顶层懒加载机制，为阅读源码建立「地图」。
2. **u1-l4 CLI 入口与命令分发**：深入 `cli_main` 的 `ROUTE_MAPPING` 路由、`use_torchrun` 多卡启动判定与 `parse_yaml_args` 配置解析——本讲已经预告了它的分发逻辑，下一讲正式拆解。
3. **u1-l5 快速上手：SFT 训练到推理全流程**：用一条 LoRA 自我认知微调命令真正跑起来，把「装好了」变成「跑通了」。

如果想立刻动手，可以先跳到 u1-l5 跑通最小示例，再回头读 u1-l3/u1-l4 理解背后机制——「先用起来，再读源码」也是有效的学习路径。
