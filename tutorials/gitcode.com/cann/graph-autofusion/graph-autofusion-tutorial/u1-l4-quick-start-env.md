# 环境搭建与快速上板运行

## 1. 本讲目标

本讲是 graph-autofusion 的「从能编译到能运行」关键一讲。读完本讲后，你应该能够：

- 看懂 CANN 软件栈的分层（驱动/固件、Toolkit、ops 算子包），知道 graph-autofusion 的 `.run` 包装在 CANN 树的哪一层，以及为什么要先装它。
- 用三种方式（CANNLab / Docker / 手动）之一准备好 CANN 环境，并用官方命令验证安装是否成功。
- 安装好 `torch_npu`，并用一行命令验证 PyTorch 已经能识别 NPU。
- 写出每次新开终端必须 `source` 的两个 `setenv`/`set_env.sh` 脚本，并说清「driver setenv」与「toolkit set_env」各自的作用，以及 `ASCEND_DEVICE_ID` 的含义。
- 用最短的 `torch.compile(options={"npu_backend": "ascendc"})` 代码使能 Autofuse，并跑通仓库自带的 `af_add_ge.py` 示例。

## 2. 前置知识

本讲承接 [u1-l3 一键构建系统 build.sh 与 CMake 工程](u1-l3-build-system.md)：上一讲我们用 `bash build.sh --pkg -j 8` 编译出了 `build_out/cann-graph-autofusion_${version}_linux-${arch}.run` 这个自安装包。本讲就从「拿到 `.run` 包之后怎么把它装进环境、怎么配运行时、怎么跑第一个融合用例」讲起。如果只想读代码、不实际跑模型，本讲的第 4.3、4.4 节仍然提供了纯源码阅读型的实践。

先用通俗语言对齐几个概念：

- **CANN**：昇腾（Ascend）芯片的统一异构计算架构，全称 Compute Architecture for Neural Networks。它是 NPU 上的「CUDA 生态」——提供编译器、运行时库、算子库。graph-autofusion 本身只依赖 AscendC 与 runtime（见 [u1-l1](u1-l1-project-overview.md)），这些能力都来自 CANN。
- **驱动与固件（driver + firmware）**：最贴近硬件的一层。驱动负责管理 `/dev/davinci0` 这类设备文件，固件跑在芯片上。**只有要真正上板运行模型时才需要装它们**；只做源码编译可以不装。
- **CANN Toolkit（开发套件包）**：编译器 + 运行时头文件/动态库。graph-autofusion 编译时链接的就是 Toolkit 里的库，编译出的 `.run` 包也安装到 Toolkit 同一棵目录树下。
- **CANN ops（算子包）**：各芯片型号（如 `910b`、`950`）的高性能算子实现，按 `--chip-type` 选择。
- **`torch_npu`**：PyTorch 官方生态里的「NPU 适配层」。它让 `torch` 能把张量和算子发到 NPU 上执行，并让 `torch.compile` 能选择 `ascendc` 后端。
- **上板（on-board）**：把程序真正放到 NPU 卡上跑，区别于「只在 CPU 上模拟」。本讲标题里的「快速上板运行」就是指跑通一个真实使用 NPU 的样例。

> 一个贯穿全讲的直觉：运行一个 Autofuse 模型，环境是**自底向上**搭起来的——先有硬件（驱动/固件），再装 CANN Toolkit+ops，再把 graph-autofusion 的 `.run` 增量安装进去，最后装 `torch_npu`。每一层都靠对应的 `setenv` 脚本把自己的库「注册」进环境变量。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `docs/zh/quick_install.md` | CANN 环境部署官方文档 | 三种安装方式、手动安装 Toolkit/ops 步骤 |
| `docs/zh/build.md` | 源码构建官方文档 | 安装 `.run` 包、环境变量配置、UT/ST 前置警告 |
| `autofuse/README.md` | Autofuse 组件说明 | `torch_npu` 安装、设置环境变量、使能 Autofuse、DFX 调测 |
| `scripts/init_env.sh` | 一键环境初始化脚本 | Docker 场景下自动装 CANN + 依赖的默认参数 |
| `autofuse/examples/pytorch/af_pointwise/af_add_ge.py` | 最小 Autofuse 用例（add+ge 融合） | `torch.compile` 使能 AscendC 后端的完整代码 |
| `autofuse/examples/pytorch/README.md` | PyTorch 用例总说明 | 前置条件、执行方式、预期结果 |
| `docs/env_install/pytorch/env_pytorch.md` | torch_npu 环境部署文档 | 虚拟环境、torch_npu 安装、验证 |

> 提示：本讲引用的所有行号基于当前 HEAD `00627d97`。本讲不改动任何源码，所有命令均为只读/安装类操作。

## 4. 核心概念与源码讲解

### 4.1 CANN 环境准备

#### 4.1.1 概念说明

在跑 Autofuse 用例之前，机器上必须先有一套可用的 CANN 环境。CANN 软件栈是分层的，理解这个分层，就能理解后面每一行 `source` 命令到底注册了什么：

```
┌──────────────────────────────────────────────┐
│  torch / torch_npu  （深度学习框架）          │  ← 4.2 节安装
├──────────────────────────────────────────────┤
│  graph-autofusion 的 .run 包（增量）          │  ← 4.1 末尾安装
├──────────────────────────────────────────────┤
│  CANN Toolkit + ops 算子包（编译器/运行时/算子）│  ← 4.1 节安装
├──────────────────────────────────────────────┤
│  NPU 驱动 + 固件（管理 /dev/davinci0）        │  ← 仅上板需要
├──────────────────────────────────────────────┤
│  昇腾 NPU 硬件                                │
└──────────────────────────────────────────────┘
```

关键认知有三点：

1. **驱动/固件是上板依赖**：只做源码编译可以不装驱动固件，但只要想真正在 NPU 上跑模型，就必须先装好驱动固件。
2. **graph-autofusion 的 `.run` 是「增量」**：它不是独立软件，而是把自己增强的 Autofuse 能力安装进 CANN Toolkit 的同一棵目录树里。
3. **安装方式有三种**：CANNLab（云环境，免装）、Docker（一键部署）、手动安装（灵活）。三者覆盖了「有没有昇腾设备」的不同场景。

#### 4.1.2 核心流程

CANN 环境准备的整体流程：

```
选择安装方式
  ├─ CANNLab  → 网页点击「CANNLab」按钮，云上即开即用（默认最新商发版 CANN）
  ├─ Docker   → 拉镜像 → docker run → 容器内执行 init_env.sh
  └─ 手动安装 → 装驱动固件(可选) → 装 Toolkit → 装 ops 包 → source set_env.sh
       │
       ▼
验证安装：cat ascend_toolkit_install.info / ascend_ops_install.info
       │
       ▼
增量安装 graph-autofusion 的 .run 包（承接 u1-l3 的编译产物）
```

#### 4.1.3 源码精读

**三种安装方式的选择表**：[docs/zh/quick_install.md:9-13](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L9-L13) 列出了 CANNLab、Docker、手动安装三种方式及其适用场景——没有昇腾设备用 CANNLab，有设备想快速搭建用 Docker，想体验 master 最新能力用手动安装。

**Docker 方式的两个场景**：[docs/zh/quick_install.md:60-65](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L60-L65) 区分了「仅编译构建」（`docker run` 不映射设备）和「需要运行样例」（必须映射 `/dev/davinci0` 等设备文件）。运行 Autofuse 用例属于后者。

**容器内一键初始化**：进入容器后执行 `curl ... init_env.sh | bash`（[docs/zh/quick_install.md:109-114](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L109-L114)）。这个脚本会自动下载并安装 CANN 包与算子包，默认参数在 [scripts/init_env.sh:19-22](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/scripts/init_env.sh#L19-L22)：`CANN_VERSION="9.0.0"`、`CHIP_TYPE="910b"`、默认安装 ops 包。其他芯片型号用 `--chip-type 950` / `--chip-type A3` 指定（[scripts/init_env.sh:266-284](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/scripts/init_env.sh#L266-L284)）。

**手动安装 Toolkit 与 ops**：[docs/zh/quick_install.md:135-145](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L135-L145) 给出 Toolkit 安装命令 `./Ascend-cann-toolkit_${version}_linux-${arch}.run --install --install-path=${install_path}`；[docs/zh/quick_install.md:146-155](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L146-L155) 给出 ops 算子包安装命令（包名含 `${soc_name}` 芯片型号）。注意 Toolkit 与 ops 必须装到**相同路径**。

**验证 CANN 安装**：装完后用 `cat` 查看安装信息文件确认版本（[docs/zh/build.md:11-16](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L11-L16)），分别对应 Toolkit 的 `ascend_toolkit_install.info` 与 ops 的 `ascend_ops_install.info`。

**增量安装 graph-autofusion 的 `.run` 包**：承接 u1-l3，编译产物是 `build_out/cann-graph-autofusion_${version}_linux-${arch}.run`。安装命令见 [docs/zh/build.md:270-272](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L270-L272)：`./build_out/cann-graph-autofusion_${version}_linux-${arch}.run --full --quiet --pylocal`。其中 `--pylocal` 会把包内 `.whl` 装到 CANN 安装路径下的 `cann/python/site-packages`，与 Toolkit 安装路径保持一致。

> ⚠️ 关键警告：[docs/zh/build.md:262-263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L262-L263) 明确指出，**执行 UT/ST 或跑样例前必须先安装这个 `.run` 包**。否则运行时 `LD_LIBRARY_PATH` 会加载到 CANN 路径下的旧版本动态库，出现 `undefined symbol` 错误。这是初学者最常见的「环境明明装了却跑不起来」的原因。

#### 4.1.4 代码实践

**实践目标**：用官方脚本检查编译环境是否就绪，理解 `[PASS]/[WARNING]/[ERROR]` 三态。

操作步骤：

1. 在仓库根目录执行环境检查脚本：`bash scripts/check_env.sh`（脚本说明见 [docs/zh/build.md:127-131](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L127-L131)）。
2. 查看每行的状态标记：`[PASS]` 表示通过、`[WARNING]` 表示非关键依赖有偏差（不影响核心编译）、`[ERROR]` 表示关键依赖缺失（必须修复）。
3. 装好 CANN 后，用 `cat /usr/local/Ascend/cann/<arch>-linux/ascend_toolkit_install.info` 确认 Toolkit 版本（命令见 [docs/zh/build.md:13](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L13)）。

需要观察的现象：

- `check_env.sh` 输出里 CANN Toolkit 版本应为 `9.0.0` 及以上（与 [scripts/init_env.sh:19](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/scripts/init_env.sh#L19) 默认值一致）。
- `cat` 安装信息文件应输出一串版本号，不报 `No such file`。

预期结果：`[ERROR]` 项为 0；至少能 `cat` 出 Toolkit 的 `install.info`。

> 待本地验证：实际安装路径可能不是默认的 `/usr/local/Ascend`（非 root 用户是 `${HOME}/Ascend`），请按自己的安装路径替换；WebIDE 场景需把 `/usr/local` 替换为 `/home/developer`（见 [docs/zh/build.md:12](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L12)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么「只做源码编译」可以不装驱动固件，而「跑样例」必须装？

**参考答案**：编译是把源码编成 `.run` 包，这个过程只调用 CANN Toolkit 的编译器与头文件，不访问真实硬件。跑样例则需要把 kernel 下发到 NPU 执行，必须通过驱动固件管理 `/dev/davinci0` 设备文件（[docs/zh/quick_install.md:123-125](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L123-L125)）。

**练习 2**：`init_env.sh` 默认装哪颗芯片的 ops 包？想跑昇腾 950 要加什么参数？

**参考答案**：默认 `CHIP_TYPE="910b"`（[scripts/init_env.sh:20](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/scripts/init_env.sh#L20)）。跑 950 需用 `--chip-type 950` 指定（用法见 [scripts/init_env.sh:271](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/scripts/init_env.sh#L271)）。

---

### 4.2 torch_npu 安装

#### 4.2.1 概念说明

CANN 装好后，机器具备了「编译 + 运行 NPU 程序」的能力，但 PyTorch 本身并不知道 NPU 的存在。`torch_npu` 就是补上这一层的适配包——它做了两件事：

1. 让 `torch` 能创建 NPU 张量、把算子调度到 NPU 上（例如 `tensor.to("npu:0")`、`torch.npu.set_device`）。
2. 为 `torch.compile` 提供 `ascendc` 后端，使 Autofuse 能接管图编译。

安装 `torch_npu` 时会自动把它依赖的 `torch` 也装上，所以通常不需要单独装 PyTorch。

#### 4.2.2 核心流程

```
1. 准备 Python 虚拟环境（建议）
2. 安装基础依赖：numpy / pyyaml / setuptools
3. pip 安装 torch_npu（自动带装 torch）
       │
       ▼
4. 验证：import torch, torch_npu; 打印 __version__
```

#### 4.2.3 源码精读

**版本要求**：Autofuse 要求 `torch_npu` 版本为 `2.9.0` 及以上，CANN 包为 `9.0.0` 及以上（[autofuse/examples/pytorch/README.md:37-41](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L37-L41)）。注意 PyTorch 用例 README 里写的是 `9.0.0`，与 `init_env.sh` 默认值一致。

**最小安装命令**：[autofuse/README.md:44-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L44-L50) 给出 `pip3 install torch_npu==2.10.0`，注释里强调「`torch_npu` 版本应为 `2.9.0` 及以上，pip 安装时会自动安装依赖的 torch 版本」。

**其他系统依赖**：[autofuse/README.md:53-65](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L53-L65) 还要求 `CMake >= 3.16.0`、`GCC >= 7.3.0`（这两个主要是编译场景需要）。

**详细的 torch_npu 部署（含虚拟环境）**：如果需要严格管理 Python 版本（`torch_npu` Daily 版对 Python 版本敏感），可参考 [docs/env_install/pytorch/env_pytorch.md:92-119](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/env_install/pytorch/env_pytorch.md#L92-L119)，用 pyenv 装 Python 3.11.4 再建虚拟环境。该文档还提供一键脚本 `scripts/env_install/pytorch/setup_torch_npu_daily.sh`（[docs/env_install/pytorch/env_pytorch.md:243-250](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/env_install/pytorch/env_pytorch.md#L243-L250)）。

**验证安装**：[docs/env_install/pytorch/env_pytorch.md:227-239](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/env_install/pytorch/env_pytorch.md#L227-L239) 给出验证代码，`import torch; import torch_npu` 后打印两者的 `__version__`。

#### 4.2.4 代码实践

**实践目标**：确认 `torch_npu` 已经装好，且 `torch` 能成功导入它。

操作步骤：

1. 在已激活的虚拟环境里执行验证脚本（来自 [docs/env_install/pytorch/env_pytorch.md:232-238](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/env_install/pytorch/env_pytorch.md#L232-L238)）：
   ```bash
   python - <<EOF
   import torch
   import torch_npu
   print("torch:", torch.__version__)
   print("torch_npu:", torch_npu.__version__)
   EOF
   ```

需要观察的现象：终端打印 `torch:` 与 `torch_npu:` 两个版本号，`torch_npu` 版本应 ≥ `2.9.0`。

预期结果：无 `ImportError`，两个版本号正常输出。

> 待本地验证：若 `import torch_npu` 报错，常见原因是 CANN 环境变量未 `source`（4.3 节），或 `torch_npu` 与已装 `torch` 版本不匹配。

#### 4.2.5 小练习与答案

**练习 1**：为什么安装 `torch_npu` 时通常不需要单独 `pip install torch`？

**参考答案**：`pip3 install torch_npu` 会自动解析并安装它依赖的 `torch`（[autofuse/README.md:49](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L49) 注释明确说明）。

**练习 2**：若 `import torch_npu` 报 `undefined symbol`，最可能漏了哪一步？

**参考答案**：最可能是没安装 graph-autofusion 的 `.run` 包，或没 `source` CANN 的 `set_env.sh`，导致加载到旧版本动态库（警告见 [docs/zh/build.md:262-263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L262-L263)）。

---

### 4.3 环境变量设置

#### 4.3.1 概念说明

装好 CANN 与 `torch_npu` 后，每次新开终端还需要执行几条 `source`/`export`，把各层的库「注册」进当前 shell 的环境变量。这是最容易被忽略、却又最关键的一步。理解的关键是区分三个来源：

| 命令 | 来源层 | 作用 |
|---|---|---|
| `source .../driver/bin/setenv.sh` | NPU 驱动 | 注册驱动库，建立与 `/dev/davinci0` 等设备的通信 |
| `source .../ascend-toolkit/set_env.sh`（或 `cann/set_env.sh`） | CANN Toolkit | 注册编译器、运行时库、头文件、Python 路径 |
| `export ASCEND_DEVICE_ID=0` | 用户设置 | 指定程序默认使用第几张 NPU 卡 |

> 术语澄清：驱动脚本是 `setenv.sh`（无下划线），Toolkit 脚本是 `set_env.sh`（有下划线）。文件名相近但分属两层，初学者容易混淆。两者的安装路径也可能不同（driver 在 `/usr/local/Ascend/driver/`，Toolkit 在 `/usr/local/Ascend/ascend-toolkit/` 或 `cann/` 下）。

#### 4.3.2 核心流程

每次新开终端的标准启动流程：

```
1. source <driver>/bin/setenv.sh        # 注册驱动层（上板必需）
2. source <toolkit>/set_env.sh          # 注册 Toolkit 层（编译+运行必需）
3. export ASCEND_DEVICE_ID=0            # 选 0 号卡（与用例脚本保持一致）
       │
       ▼
4. 验证：python -c "import torch, torch_npu"  # 不报错即环境就绪
```

#### 4.3.3 源码精读

**Autofuse README 给出的标准三行**：[autofuse/README.md:67-77](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L67-L77) 明确列出了执行用例前必须设置的环境变量：

```bash
# 用户自己的 driver 包安装路径
source /usr/local/Ascend/driver/bin/setenv.sh
# 用户自己的 CANN 包安装路径
source /usr/local/Ascend/ascend-toolkit/set_env.sh
# 假设跑在 0卡，和脚本保持一致
export ASCEND_DEVICE_ID=0
```

注意注释强调「假设跑在 0 卡，和脚本保持一致」——`ASCEND_DEVICE_ID` 必须与示例脚本里硬编码的设备号（如 `"npu:0"`）对应，否则会跑到别的卡上或找不到设备。

**build.md 的环境变量配置**：[docs/zh/build.md:18-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L18-L27) 给出 Toolkit 的 `set_env.sh` 调用，默认路径 `source /usr/local/Ascend/cann/set_env.sh`，指定路径则 `source ${install_path}/cann/set_env.sh`。这里路径是 `cann/set_env.sh`，与 Autofuse README 的 `ascend-toolkit/set_env.sh` 不同——两者都是合法的 Toolkit 入口，具体取决于 CANN 包的安装布局，以你机器上实际存在的文件为准。

**ASCEND_DEVICE_ID 在用例中的呼应**：示例脚本里 `DEVICE = "npu:0"` 并 `torch.npu.set_device(DEVICE)`（[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:18-19](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L18-L19)），与 `ASCEND_DEVICE_ID=0` 保持一致。

#### 4.3.4 代码实践

**实践目标**：把环境变量设置固化为一个可复用的启动脚本，并说明每个变量的作用。

操作步骤：

1. 在仓库根目录新建一个 `setup_runtime_env.sh`（示例代码，非项目原有文件）：
   ```bash
   #!/bin/bash
   # ===== 示例代码：运行 Autofuse 用例前的环境启动脚本 =====
   # 1. 驱动层：注册设备通信所需库（上板必需）
   source /usr/local/Ascend/driver/bin/setenv.sh
   # 2. Toolkit 层：注册编译器与运行时库（按实际路径二选一）
   source /usr/local/Ascend/ascend-toolkit/set_env.sh   # 或 cann/set_env.sh
   # 3. 选 0 号卡（与示例脚本 npu:0 一致）
   export ASCEND_DEVICE_ID=0
   ```
2. 赋予执行权限后，每次新开终端执行 `source setup_runtime_env.sh`。
3. 执行 `python3 -c "import torch, torch_npu; print(torch.npu.is_available())"` 验证。

需要观察的现象：最后一条命令应打印 `True`，表示 PyTorch 已识别到可用的 NPU 设备。

预期结果：`is_available()` 返回 `True`，说明驱动层与 Toolkit 层都已正确注册。

> 待本地验证：`set_env.sh` 的确切路径取决于 CANN 安装布局，请用 `ls /usr/local/Ascend/` 实际查看后再填。若 `is_available()` 为 `False`，通常是驱动 `setenv.sh` 未 `source` 或设备未挂载。

#### 4.3.5 小练习与答案

**练习 1**：`setenv.sh`（驱动）和 `set_env.sh`（Toolkit）文件名差一个下划线，它们分别属于哪一层？能否只 `source` 其中一个就跑通样例？

**参考答案**：`setenv.sh` 属驱动层（`driver/bin/`），`set_env.sh` 属 Toolkit 层（`ascend-toolkit/` 或 `cann/`）。跑样例需要同时 `source` 两者：缺驱动层会找不到设备，缺 Toolkit 层会缺运行时库（来源 [autofuse/README.md:67-77](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L67-L77)）。

**练习 2**：把 `ASCEND_DEVICE_ID` 设成 `1`，但示例脚本里写的是 `npu:0`，会发生什么？

**参考答案**：`ASCEND_DEVICE_ID` 控制默认设备，但示例脚本显式调用了 `torch.npu.set_device("npu:0")`（[af_add_ge.py:18-19](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L18-L19)），所以仍跑在 0 号卡。两者不一致时以脚本显式设置为准；若机器只有一张卡，`ASCEND_DEVICE_ID=1` 还可能直接报错。故 README 注释强调「和脚本保持一致」。

---

### 4.4 运行第一个 Autofuse 用例

#### 4.4.1 概念说明

环境就绪后，最关键的一步是：**如何用一行代码让 Autofuse 接管模型编译？** 答案是 `torch.compile` 配合 `ascendc` 后端。Autofuse 不需要你单独 `import` 任何模块，只需在 `torch.compile` 的 `options` 里指定 `npu_backend: "ascendc"`，框架就会把可融合的算子交给 Autofuse 后端处理。

仓库自带的最小用例 `af_add_ge.py` 演示了「`add` + `ge`（加法 + 比较大于等于）融合」：原本 `torch.ge(torch.add(x, y), z)` 是两个独立的 Vector 算子，中间有一次全局内存搬运；Autofuse 会把它们融合成一个名为 `autofused_` 的单一 kernel，消除这次搬运。

#### 4.4.2 核心流程

一个 Autofuse 用例从代码到上板的流程：

```
用户代码：torch.compile(model, options={"npu_backend":"ascendc"})
       │
       ▼
首次调用 model(x, y, z)
       │  触发图捕获 + 编译
       ▼
torch_npu 把图发到 ascendc 后端
       │
       ▼
Autofuse 后端：融合范围识别 → tiling → codegen
       │  生成融合 kernel（产物目录名以 autofused_ 开头）
       ▼
kernel 下发到 NPU 执行
       │
       ▼
profiling 目录生成 op_summary CSV，可见 autofused_ kernel
```

#### 4.4.3 源码精读

**模型定义（待融合的两个算子）**：[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:23-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L23-L29) 定义了 `MyModel`，其 `forward` 是 `result = torch.ge(torch.add(x, y), z)`——`add` 的输出直接喂给 `ge`，是典型的可融合 pointwise 模式。

**使能 Autofuse 的核心三行**：[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) 即本讲最核心的代码：

```python
model = torch.compile(
    model,
    dynamic=False,
    fullgraph=True,
    options={"npu_backend": "ascendc"},
)
```

其中 `options={"npu_backend": "ascendc"}` 就是使能 Autofuse 的开关。`dynamic=False` 表示固定 shape，`fullgraph=True` 要求整图编译。

**复杂网络使能方式**：[autofuse/README.md:151-162](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L151-L162) 说明，在真实网络里使能 Autofuse 无需单独导入任何额外模块，同样只需在 `torch.compile` 指定 AscendC 后端。

**执行方式**：用例直接 `python3 test.py` 即可（[autofuse/README.md:79-82](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L79-L82)），如 `cd af_pointwise && python af_add_ge.py`（[autofuse/examples/pytorch/README.md:63-68](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L63-L68)）。

**预期结果判定**：[autofuse/examples/pytorch/README.md:84-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L84-L100) 说明，程序执行后生成 `profiling` 目录，查看 `op_summary_时间戳.csv`，若算子列表中存在以 `autofused_` 开头的 kernel，即说明融合成功。

**DFX 调测（排查未融合原因）**：若没看到 `autofused_` 目录，可开 `TORCH_COMPILE_DEBUG=1`（[autofuse/README.md:84-91](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L84-L91)），在 `torch_compile_debug` 目录里查看融合产物；终端的 `Fallback aten.xxxx $reason` 会告诉你某个算子为何回退到未融合状态（更详细的 DFX 将在 [u3-l3 框架使能与 DFX 调测](u3-l3-enable-and-dfx.md) 展开）。

#### 4.4.4 代码实践

**实践目标**：编写一段最短的 `torch.compile(options={"npu_backend":"ascendc"})` 代码（承接本讲开篇的实践任务），并说明关键环境变量。

操作步骤：

1. 在已 `source` 环境变量（见 4.3）的终端里，新建 `mini_autofuse.py`（示例代码，非项目原有文件）：
   ```python
   # ===== 示例代码：最短的 Autofuse 使能用例 =====
   import torch
   import torch_npu  # noqa: F401  导入即注册 NPU 后端

   DEVICE = "npu:0"
   torch.npu.set_device(DEVICE)

   class Model(torch.nn.Module):
       def forward(self, x, y, z):
           return torch.ge(torch.add(x, y), z)  # add + ge 可融合

   model = torch.compile(
       Model().to(DEVICE),
       dynamic=False,
       fullgraph=True,
       options={"npu_backend": "ascendc"},      # 使能 Autofuse 的关键
   )

   x = torch.randn(128, 50, device=DEVICE)
   y = torch.randn(128, 50, device=DEVICE)
   z = torch.randn(128, 50, device=DEVICE)
   out = model(x, y, z)        # 首次调用触发编译
   print("output shape:", out.shape)
   ```
2. 运行：`python3 mini_autofuse.py`。
3. 若想观察融合产物，先 `export TORCH_COMPILE_DEBUG=1` 再运行，然后在生成的 `torch_compile_debug` 目录下查找以 `autofused_` 为前缀的目录（判定依据见 [autofuse/README.md:131-132](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L131-L132)）。

需要观察的现象：

- 终端正常打印 `output shape: torch.Size([128, 50])`，说明融合 kernel 成功在 NPU 上跑通。
- 开启 `TORCH_COMPILE_DEBUG` 后，`torch_compile_debug` 下出现 `autofused_*` 目录，即融合成功；若只看到 `Fallback aten.xxxx $reason`，说明该算子未被融合。

**关键环境变量说明**（本实践任务要求）：

- `ASCEND_DEVICE_ID=0`：指定默认使用 0 号 NPU 卡，须与代码里的 `npu:0` 一致（见 4.3.3）。
- `source .../driver/bin/setenv.sh`：注册驱动层，使程序能访问 `/dev/davinci0` 等设备。
- `source .../ascend-toolkit/set_env.sh`：注册 Toolkit 运行时库与编译器，`torch_npu` 与 Autofuse 都依赖它。
- `TORCH_COMPILE_DEBUG=1`（可选）：开启后落盘编译中间产物，是判断「是否融合」「为何 fallback」的主要手段。

预期结果：融合成功时，运行无报错且 `torch_compile_debug` 下存在 `autofused_` 产物目录。

> 待本地验证：本实践依赖真实 NPU 与已安装的 `.run` 包；无设备时无法看到 `autofused_` 产物，但可对照 [af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) 做纯源码阅读，确认使能 Autofuse 的最小要素就是 `options={"npu_backend": "ascendc"}` 这一行。

#### 4.4.5 小练习与答案

**练习 1**：去掉 `options={"npu_backend": "ascendc"}`，模型还能在 NPU 上跑吗？还能融合吗？

**参考答案**：仍可能在 NPU 上跑（`torch_npu` 提供基本调度），但 `torch.compile` 会走默认（Inductor）路径，不会进入 Autofuse 后端，因此不会生成 `autofused_` 融合 kernel。`options` 里的 `npu_backend: "ascendc"` 才是切换到 Autofuse 的开关（[af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38)）。

**练习 2**：运行后 `torch_compile_debug` 下没有 `autofused_` 目录，应从哪里找原因？

**参考答案**：查看终端输出的 `Fallback aten.xxxx $reason: ...` 信息（[autofuse/README.md:131-132](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L131-L132)），它会说明哪个算子、因为什么原因回退到了未融合的单算子形态。

**练习 3**：`af_add_ge.py` 为什么要跑 100 步（`for _ in range(100)`）而不是 1 步？

**参考答案**：首步触发编译并下发融合 kernel，后续步复用已编译结果；跑 100 步并在 profiling 区间内（[af_add_ge.py:70-72](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L70-L72)）是为了采集稳定的 kernel 性能数据，消除首次编译耗时对 profiling 的干扰。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，从一台「只装了 OS」的机器（或一个干净容器）出发，完成「装环境 → 装包 → 设变量 → 跑融合用例 → 看融合产物」全流程，并用一句话总结每一层环境的作用。

建议步骤（可按实际条件选 Docker 或手动方式）：

1. **装 CANN**：在容器内执行 `curl -fsSL https://raw.gitcode.com/cann/graph-autofusion/raw/master/scripts/init_env.sh | bash`（来源 [docs/zh/quick_install.md:113](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L113)），或参考 [docs/zh/quick_install.md:135-155](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L135-L155) 手动安装 Toolkit + ops。用 `cat .../ascend_toolkit_install.info` 验证（[docs/zh/build.md:13](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L13)）。
2. **装 graph-autofusion 的 `.run` 包**：承接 u1-l3 的编译产物，执行 [docs/zh/build.md:271](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L271) 的 `--full --quiet --pylocal` 安装命令。务必先装它再跑用例（警告见 [docs/zh/build.md:262-263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L262-L263)）。
3. **装 torch_npu**：`pip3 install torch_npu==2.10.0`（[autofuse/README.md:46-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L46-L50)），用 [docs/env_install/pytorch/env_pytorch.md:232-238](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/env_install/pytorch/env_pytorch.md#L232-L238) 验证版本。
4. **设环境变量**：`source` 驱动 `setenv.sh` 与 Toolkit `set_env.sh`，`export ASCEND_DEVICE_ID=0`（[autofuse/README.md:70-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L70-L76)）。
5. **跑用例**：`cd autofuse/examples/pytorch/af_pointwise && export TORCH_COMPILE_DEBUG=1 && python af_add_ge.py`。
6. **看产物**：在 `torch_compile_debug` 下确认存在 `autofused_` 目录（判定见 [autofuse/examples/pytorch/README.md:96-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L96-L100)）。

**输出要求**：用一句话总结四层环境各自的作用——驱动固件（管设备）、CANN Toolkit+ops（给编译器与运行时）、graph-autofusion `.run`（增强 Autofuse 能力）、torch_npu（让 PyTorch 识别 NPU 并选 ascendc 后端）。

> 待本地验证：完整流程需真实 NPU 设备；无设备时，可只完成步骤 1-3 的安装与验证，并在步骤 5 改为纯源码阅读，对照 [af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) 理解使能逻辑。

## 6. 本讲小结

- 运行 Autofuse 模型的环境是自底向上搭起来的：NPU 驱动/固件（上板必需）→ CANN Toolkit + ops（编译器与运行时）→ graph-autofusion `.run` 增量包 → torch_npu（PyTorch 适配层）。
- CANN 有三种安装方式：CANNLab（云，免装）、Docker（一键，容器内跑 `init_env.sh`）、手动安装（最灵活）；手动安装时 Toolkit 与 ops 须装到相同路径（[docs/zh/quick_install.md:135-155](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/quick_install.md#L135-L155)）。
- **跑用例前必须先安装 graph-autofusion 的 `.run` 包**，否则会因加载旧动态库报 `undefined symbol`（[docs/zh/build.md:262-263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L262-L263)）。
- 每次新开终端要 `source` 两层脚本：驱动的 `setenv.sh` 与 Toolkit 的 `set_env.sh`，并 `export ASCEND_DEVICE_ID=0` 与用例脚本保持一致（[autofuse/README.md:67-77](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L67-L77)）。
- 使能 Autofuse 只需 `torch.compile(..., options={"npu_backend": "ascendc"})` 一行（[af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38)）；融合成功与否看 `torch_compile_debug` 下的 `autofused_` 目录与 profiling 的 `op_summary` CSV。

## 7. 下一步学习建议

入门层（u1-u3）到此结束，你已经能跑通 Autofuse。建议按以下方向继续：

1. **u2 SuperKernel 组件入门**：本讲聚焦 Autofuse 用例，SuperKernel 是与之正交的另一组件，可独立学习其原理与 JIT 入口。
2. **u3-l3 框架使能与 DFX 调测**：本讲只用了 `TORCH_COMPILE_DEBUG` 做最基础的排查，进阶的 `AUTOFUSE_DFX_FLAGS`、profiling 性能对比、fallback 原因分析将在该讲深入（预览见 [autofuse/README.md:111-130](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L111-L130)）。
3. **动手读 `af_mul_reducesum.py` 与 `af_gather_add.py`**：仓库还有 reduce 融合、gather+add 图模式两个用例（见 [autofuse/examples/pytorch/README.md:19-35](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L19-L35)），可作为熟悉不同融合模式的练习。
4. **进入 Autofuse 数据流主线**：u4 起将沿 graph_metadef → ascir → optimize → att → codegen → compiler 逐层精读源码，建议先回顾 [u3-l2 Autofuse 目录结构与六大模块总览](u3-l2-autofuse-overview.md) 建立地图。
