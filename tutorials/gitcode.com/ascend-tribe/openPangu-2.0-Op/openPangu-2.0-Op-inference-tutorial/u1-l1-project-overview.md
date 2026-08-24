# 项目是什么：昇腾融合推理算子库全貌

## 1. 本讲目标

本讲是整个学习手册的第一篇，不涉及任何一行 Kernel 代码，目标只有三个：

1. 能用一两句话说清楚 **openPangu-2.0-Op 这个仓库是干什么的**，以及它的两大组成部分：AscendC 融合推理算子库与 `torch_ops_extension` PyTorch 扩展。
2. 能对照目录树，**不看文档** 直接说出 `inference/ascendc` 下每个一级目录（`cmake`、`scripts`、`src`、`torch_ops_extension`、`build.sh`、`CMakeLists.txt`）的职责。
3. 能在 `src` 下准确定位六大算子族（attention / mhc / index / posembedding / moe / ops-nn 的 matmul）的位置，并理解 AscendC、PyPTO、Triton 三种算子实现方式的定位差异。

学完本讲，你拿到一份 bug 报告说「某个 `npu_xxx` 算子有问题」，应该能立刻在仓库里找到它对应的目录。

## 2. 前置知识

本讲假设你没有昇腾开发经验，只需要具备基本的 Linux 命令行和 Python/PyTorch 常识。下面几个术语会用通俗语言解释清楚：

- **NPU / 昇腾（Ascend）**：华为的神经网络处理器，类似 GPU，专门用来跑深度学习计算。本仓库的算子全部运行在昇腾 NPU 上。常见型号有 910B（Atlas A2 训练卡）、910C（Atlas A3）、950PR（Atlas A5）。
- **算子（Operator，简称 Op）**：深度学习框架里最小粒度的计算单元，比如矩阵乘、加法、注意力。PyTorch 里 `torch.matmul` 就是一个算子。
- **自定义算子**：框架内置算子覆盖不到的融合计算（例如把 RMSNorm + RoPE + 写 KV Cache 三步融合成一步），需要开发者自己写。性能敏感的大模型推理场景大量依赖自定义算子来减少访存和 kernel 启动开销。
- **AscendC**：昇腾原生的 C/C++ 算子开发语言，可以直接操作 NPU 硬件资源（如片上 Unified Buffer），性能最优，但开发门槛也最高。本仓库推理子仓的全部算子都用 AscendC 实现。
- **CANN**：昇腾的计算架构软件栈（类似 NVIDIA 的 CUDA），提供编译器、运行时和开发工具包（Toolkit）。AscendC 算子编译安装后会被放进 CANN 的 `vendors` 目录供运行时加载。
- **aclnn 接口**：AscendCL（昇腾计算语言）层的 C 接口命名约定，每个算子对外暴露形如 `aclnnXxx` 的函数。后面第 2、3 单元会大量接触。
- **torch_npu / PTA**：PyTorch Adapter，让 PyTorch 能把张量放到 NPU 设备上的适配层。`import torch_npu` 之后 `torch.Tensor` 就能在 NPU 上运算。
- **wheel 包**：Python 的标准分发格式（`.whl`）。`torch_ops_extension` 最终会打成一个 wheel，装好后 `import omni_custom_ops` 即可用 `torch.ops.custom.npu_xxx(...)` 调用本仓库的算子。

## 3. 本讲源码地图

本讲只读两个 README 和一个 Python 入口文件，它们是理解全貌的钥匙：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md) | 仓库根 README：项目定位、整体目录树、算子列表、技术栈与硬件支持 |
| [inference/ascendc/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md) | 推理子仓 README：更细的目录结构说明、Docker 环境准备、编译与安装命令 |
| [inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py) | Python 包入口：把 `torch.ops.custom` 命名空间挂载到 `torch_npu`，是「算子如何被 PyTorch 看到」的第一现场 |

> 提示：本讲会同时引用「文档说法」和「实际目录」。两者并不完全一致（见 4.3 节），这是阅读真实仓库时非常重要的意识——**以源码目录为准，文档可能滞后**。

## 4. 核心概念与源码讲解

### 4.1 项目定位：这是一份什么样的仓库

#### 4.1.1 概念说明

openPangu-2.0-Op 是一个**昇腾亲和高性能自定义算子仓库**，服务于盘古系列大模型的训练与推理。它要解决的问题很具体：大模型推理时，通用框架算子的性能和功能都不够用，需要为特定计算模式（稀疏注意力、MHC 结构、MoE 路由等）手工编写融合算子。

仓库按场景分成两大顶层目录：

- `training/`：训练场景算子（含 ascendc、pypto、triton 三种实现），不在本手册范围内。
- `inference/`：**推理场景算子**，也就是本手册分析的对象。

`inference/` 下只有一个子目录 `ascendc/`，它内部又分成两大组成部分，这是本讲最重要的一个认知：

1. **AscendC 融合推理算子库**（`inference/ascendc/src` + 构建系统）：C/C++ 实现的算子本体，编译后生成 CANN 自定义算子 run 包，安装进昇腾工具链。
2. **torch_ops_extension**（`inference/ascendc/torch_ops_extension`）：PyTorch 扩展层，把上面的算子包装成 Python 可调用的 `torch.ops.custom.npu_*` 接口，打成 wheel 包。

一句话概括两者关系：**AscendC 算子库是「发动机」，torch_ops_extension 是「方向盘」**。

仓库根 README 还声明了三种算子实现方式的定位差异：

| 实现方式 | 定位 | 在本仓库中的位置 |
|----------|------|------------------|
| AscendC | 昇腾原生 C/C++ 开发框架，直接操作 NPU 硬件资源，性能最优 | `inference/ascendc`（本手册主角）、`training/ascendc` |
| Triton | 基于 Triton 语言的开发方式，适合快速原型验证 | `training/triton` |
| PyPTO | 基于 PyTorch 的算子开发方式 | `training/pypto`（quant 量化算子） |

注意：**推理场景（inference/）目前只有 AscendC 一种实现**，Triton 与 PyPTO 只出现在训练场景。这就是本手册聚焦 AscendC 的原因。

#### 4.1.2 核心流程

从「仓库代码」到「用户调用」的宏观链路可以这样理解：

```text
┌─────────────────────────────── 仓库（inference/ascendc） ───────────────────────────────┐
│                                                                                          │
│  ① src/ 下每个算子目录（op_api / op_host / op_kernel 三层）                              │
│        │  bash build.sh -c <soc_version>                                                 │
│        ▼                                                                                │
│  ② 编译产出 CANN-omni_custom_ops-<版本>-linux.<arch>.run（自定义算子 run 包）             │
│        │  安装到 /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/                   │
│        ▼                                                                                │
│  ③ torch_ops_extension 编译产出 omni_custom_ops-*.whl（Python wheel 包）                 │
│        │  pip install                                                                   │
│        ▼                                                                                │
│  ④ 用户代码：import omni_custom_ops 后                                                   │
│     torch.ops.custom.npu_xxx(...)  或  torch_npu.npu_xxx(...)                            │
│        │  wheel 里的 csrc 适配层通过 dlopen/dlsym 调用已安装 run 包中的 aclnnXxx C 接口    │
│        ▼                                                                                │
│  ⑤ NPU 上执行真实的 AscendC kernel                                                       │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

本讲只需要建立这个「双包协作」的整体印象：run 包管算子本体，wheel 包管 Python 接口。每一层的内部细节留给后续单元。

#### 4.1.3 源码精读

先看仓库根 README 的第一句话，这是整个项目的定位声明：

> [README.md:L1-L3](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L1-L3)
>
> ```markdown
> # openPangu-2.0-Op
>
> 昇腾亲和高性能自定义算子仓库，面向大模型训练与推理场景，提供基于 AscendC、PyPTO、Triton 多种实现方式的高性能算子，并通过Pytorch Adapter(PTA)暴露为`torch.ops.custom.npu_*`接口，支持在Pytorch中直接调用
> ```

这段话信息量很大，拆开读：

- 「面向大模型训练与推理场景」→ 仓库分成 `training/` 与 `inference/` 两个顶层目录。
- 「基于 AscendC、PyPTO、Triton 多种实现方式」→ 三种技术栈并存，推理子仓只用 AscendC。
- 「通过 Pytorch Adapter(PTA) 暴露为 `torch.ops.custom.npu_*` 接口」→ 用户最终在 PyTorch 里以 `torch.ops.custom.npu_xxx` 的形式调用，这正是 `torch_ops_extension` 存在的意义。

技术栈的正式说明在同一份 README 的末尾部分：

> [README.md:L135-L139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L135-L139)
>
> ```markdown
> ## 技术栈
>
> - **AscendC**: 昇腾原生 C/C++ 算子开发框架，直接操作 NPU 硬件资源，性能最优
> - **Triton**: 基于 Triton 语言的算子开发，适合快速原型验证
> - **PyPTO**: 基于 PyTorch 的算子开发方式
> ```

硬件支持范围则声明在：

> [README.md:L141-L145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L141-L145)
>
> ```markdown
> ## 硬件支持
>
> - 昇腾 910B (Atlas A2)
> - 昇腾 910C (Atlas A3)
> - 昇腾 950PR (Atlas A5)
> ```

记住这三个型号，后面编译时要通过 `-c` 参数指定目标芯片，取值为 `ascend910b`（对应 A2）、`ascend910_93`（对应 A3）、`ascend950`（对应 A5）（具体用法在下一讲展开）。

最后看「方向盘」的入口——`omni_custom_ops` 包的 `__init__.py`，它的文档字符串直接写出了两种调用方式：

> [inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L8-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L8-L12)
>
> ```python
> """
> import custom ops as torch_npu ops to support the following usage:
> 'torch.ops.custom.npu_selected_flash_attention()'
> 'torch_npu.npu_selected_flash_attention()'
> """
> ```

也就是说，装好 wheel 包并 `import omni_custom_ops` 之后，`torch.ops.custom.npu_xxx(...)` 和 `torch_npu.npu_xxx(...)` 两种写法都可用。后者是怎么实现的？答案就在同一个文件的挂载逻辑里（详见 4.3.3 节的精读）。

#### 4.1.4 代码实践

**实践一：验证「双包」结构确实存在（源码阅读型实践，无需 NPU 环境）**

1. **实践目标**：亲眼确认推理子仓由「AscendC 算子库」和「torch_ops_extension」两大块组成，并找到它们的物理分界。

2. **操作步骤**：
   ```bash
   # 进入推理子仓
   cd inference/ascendc

   # 观察一级目录：src（算子本体）与 torch_ops_extension（PyTorch 扩展）并列
   ls -la

   # 确认 AscendC 算子库的入口：build.sh 负责编译算子 run 包
   head -30 build.sh

   # 确认 wheel 包的构建入口
   ls torch_ops_extension/
   # 预期看到：build_and_install.sh  omni_custom_ops  setup.py
   ```

3. **需要观察的现象**：
   - `ls -la` 输出中 `src/` 与 `torch_ops_extension/` 并列存在；
   - `torch_ops_extension/` 下有 `setup.py`（Python 打包的标准入口）和 `build_and_install.sh`（一键编译安装脚本）。

4. **预期结果**：你能指着目录树说出「左边这块编 run 包，右边这块打 wheel 包」，即完成本实践。

（本实践只做目录观察，命令输出与机器无关；`head build.sh` 的具体内容解读在下一讲进行。）

#### 4.1.5 小练习与答案

**练习 1**：仓库同时提供 AscendC、Triton、PyPTO 三种实现方式。如果要在推理场景新增一个对性能极其敏感的融合算子，应选哪种？如果只是快速验证一个想法呢？

**参考答案**：性能敏感的推理算子选 **AscendC**——它是昇腾原生 C/C++ 框架，直接操作 NPU 硬件资源（README 明确说「性能最优」），本仓库全部推理算子都采用它。快速原型验证可考虑 **Triton**（「适合快速原型验证」），但注意本仓库的 Triton 实现目前只在 `training/triton` 下存在，推理子仓没有。

**练习 2**：用户在 PyTorch 中调用本仓库算子的两种写法是什么？分别依赖哪一层？

**参考答案**：`torch.ops.custom.npu_xxx(...)` 和 `torch_npu.npu_xxx(...)`（见 `__init__.py` 第 8–12 行的文档字符串）。前者依赖 wheel 包中 csrc 适配层注册到 `custom` 命名空间的算子；后者是把前者挂载（`setattr`）到 `torch_npu` 模块上的语法糖，两种写法最终调用的是同一个函数对象。

**练习 3**：判断对错：「`inference/` 目录下除了 AscendC 实现还有 Triton 实现。」

**参考答案**：错。`inference/` 下只有 `ascendc/` 一个实现目录；Triton（`training/triton`）和 PyPTO（`training/pypto`）只出现在训练场景。可对照根 README 的项目结构树（[README.md:L7-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L7-L80)）或直接 `ls inference/` 验证。

### 4.2 目录结构：inference/ascendc 的一级地图

#### 4.2.1 概念说明

读一个构建型仓库，先看一级目录能最快建立「东西放在哪」的心智地图。`inference/ascendc` 的一级结构非常克制，只有 8 个条目，可以分成三组：

| 条目 | 组 | 职责 |
|------|----|------|
| `src/` | 源码 | **全部算子源代码** + 测试框架 + 公共工具，是阅读的主战场 |
| `torch_ops_extension/` | 源码 | PyTorch 扩展：csrc 适配层 + converter，产出 wheel 包 |
| `build.sh` | 构建系统 | 工程编译入口脚本，解析参数后调用 CMake |
| `CMakeLists.txt` | 构建系统 | 顶层 CMake 配置，定义 opapi/optiling/opsproto 等目标 |
| `cmake/` | 构建系统 | CMake 模块（`modules/`）、脚本（`scripts/`）、第三方依赖（`third_party/`），如 `tiling_sink.cmake`、`ut.cmake` |
| `scripts/` | 构建系统 | 构建/部署辅助脚本（含 `util/` 工具函数） |
| `README.md` | 文档 | 推理子仓说明：目录结构、环境准备、编译安装 |
| `version.info` | 文档 | 项目版本信息 |

其中 `src/` 还要再往里看一层，因为它内部又分了四个职责明确的区域：

| `src/` 子目录 | 职责 |
|---------------|------|
| `ops-transformer/` | Transformer 类算子，按算子族再分 `attention/`、`mhc/`、`index/`、`posembedding/`、`moe/`、`common/` |
| `ops-nn/` | NN 类算子，目前只有 `matmul/`（内含 `ai_infra_matmul` 算子与 `common/` 公共库） |
| `tests/` | 测试框架代码：`st/`（系统测试）与 `ut/`（单元测试框架） |
| `utils/` | 公共工具：`inc/`（error、log 头文件）与 `util/`（工具实现） |

#### 4.2.2 核心流程

把上面的表格拼成一棵树（省略算子层，下一节展开）：

```text
inference/ascendc/
├── build.sh                  # 编译入口：bash build.sh -c <soc> -n '<算子列表>'
├── CMakeLists.txt            # 顶层 CMake 配置
├── README.md                 # 推理子仓说明文档
├── version.info              # 版本信息
├── cmake/                    # CMake 模块/脚本/第三方依赖
├── scripts/                  # 构建部署辅助脚本
├── src/
│   ├── ops-transformer/      # Transformer 算子（六大族中的五个在这里）
│   │   ├── attention/        #   注意力族（数量最多）
│   │   ├── mhc/              #   MHC 族
│   │   ├── index/            #   索引族
│   │   ├── posembedding/     #   位置编码族
│   │   ├── moe/              #   MoE 路由族
│   │   └── common/           #   公共组件（tiling_base/fallback/tiling_sink 等）
│   ├── ops-nn/
│   │   ├── matmul/           #   matmul 算子族
│   │   └── common/           #   matmul 公共库
│   ├── tests/                # st/ + ut/ 测试框架
│   └── utils/                # error/log 等公共工具
└── torch_ops_extension/      # PyTorch 扩展（wheel 包）
    ├── build_and_install.sh  #   wheel 一键编译安装
    ├── setup.py              #   打包配置
    └── omni_custom_ops/      #   Python 包：csrc_base + 各算子 csrc/converter
```

**记忆口诀**：构建系统（build.sh/CMakeLists/cmake/scripts）负责「怎么编」，`src` 负责「算子本体」，`torch_ops_extension` 负责「怎么被 PyTorch 调用」。以后找任何文件，先问自己它属于哪一类。

#### 4.2.3 源码精读

推理子仓 README 用一整节描述了这套目录结构，开头部分如下：

> [inference/ascendc/README.md:L9-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L9-L35)
>
> ```text
> ├── cmake                                                                        # 项目工程编译目录
> |   ├── modules                                                                  # CMake 模块
> |   ├── scripts                                                                  # CMake 脚本
> |   ├── third_party                                                              # 第三方依赖
> ├── scripts                                                                      # 构建/部署辅助脚本
> |   ├── util                                                                     # 脚本工具函数
> ├── src                                                                          # 算子的源代码
> |   ├── tests                                                                    # 测试框架代码
> |   |   ├── st                                                                   # 系统测试
> |   |   ├── ut                                                                   # 单元测试框架
> |   |   |   ├── framework_normal                                                 # 标准测试框架
> |   |   |   |   ├── common                                                       # 测试公共代码
> |   |   |   |   ├── op_api                                                       # op_api 层测试支持
> |   |   |   |   ├── op_host                                                      # op_host 层测试支持
> |   |   |   |   ├── op_kernel                                                    # op_kernel 层测试支持
> |   ├── utils                                                                    # 公共工具函数
> |   |   ├── inc                                                                  # 工具头文件（error、log）
> |   |   ├── util                                                                 # 工具实现
> ```

这段 README 透露了一个后续单元的关键信息：`tests/ut/framework_normal` 下按 **op_api / op_host / op_kernel** 三个层次组织测试支持代码——这正对应每个 AscendC 算子内部的三层结构（第 1 单元第 3 讲会解剖）。

README 中关于单个算子目录的标准形态（以稀疏注意力为例）：

> [inference/ascendc/README.md:L30-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L30-L35)
>
> ```text
> |   ├── ops-transformer                                                          # transformer 算子目录
> |   |   ├── attention                                                            # attention 算子目录
> |   |   |   ├── ai_infra_sparse_flash_attention_gqa                              # 稀疏 Flash Attention (GQA) 算子
> |   |   |   |   ├── docs                                                         # 算子设计文档
> |   |   |   |   ├── op_api                                                       # 算子 API 层实现
> |   |   |   |   ├── op_host                                                      # 算子信息库、Tiling、InferShape 实现
> |   |   |   |   ├── op_kernel                                                    # 算子 Kernel 实现
> |   |   |   |   ├── tests                                                        # 算子测试（st/ut）
> ```

先记住这个「docs / op_api / op_host / op_kernel / tests」五件套的形状，下一讲我们以最小的算子为标本逐层拆开它。

另外注意 README 里的特殊一例：`ai_infra_fused_infer_attention_sink_metadata` 算子目录下不是 `op_kernel` 而是 `op_kernel_aicpu` 和 `op_graph`（[inference/ascendc/README.md:L54-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L54-L60)）——它是运行在 AICPU 而非 AICore 上的元数据算子，这是「目录结构反映执行介质」的典型例子，第 4 单元会专门讲。

torch_ops_extension 一侧的目录约定：

> [inference/ascendc/README.md:L131-L139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L131-L139)
>
> ```text
> ├── torch_ops_extension                                                          # PyTorch 算子扩展目录
> |   ├── omni_custom_ops                                                          # 推理自定义算子包
> |   |   ├── csrc_base                                                            # 自定义算子公共适配层 C++ 代码
> |   |   ├── ops_transformer                                                      # transformer 算子适配目录
> |   |   |   ├── attention                                                        # attention 算子适配目录
> |   |   |   |   ├── sparse_flash_attention_gqa                                   # 稀疏 Flash Attention GQA 适配
> |   |   |   |   |   ├── csrc                                                     # 适配层 C++ 代码
> |   |   |   |   |   ├── converter                                                # Python 侧 converter 代码
> ```

要点：`src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa`（算子本体）与 `torch_ops_extension/omni_custom_ops/ops_transformer/attention/sparse_flash_attention_gqa`（适配层）是**一一镜像**的目录关系，只是适配层目录名去掉了 `ai_infra_` 前缀。掌握这个对应规律，从任何一侧都能推出另一侧的路径。

#### 4.2.4 代码实践

**实践二：亲手画出 inference/ascendc 一级目录树并标注职责（本讲核心实践）**

1. **实践目标**：不依赖任何文档，仅通过 `ls` 命令核实目录结构，产出一棵带职责标注的目录树。

2. **操作步骤**：
   ```bash
   cd inference/ascendc

   # 第一步：列出一级条目
   ls -la
   # 预期看到：CMakeLists.txt  README.md  build.sh  cmake/  scripts/  src/  torch_ops_extension/  version.info

   # 第二步：核实 src 的四个子区域
   ls src/
   # 预期看到：ops-nn  ops-transformer  tests  utils

   # 第三步：核实 torch_ops_extension 的组成
   ls torch_ops_extension/
   # 预期看到：build_and_install.sh  omni_custom_ops  setup.py

   # 第四步：（可选）用 tree 命令限制深度输出，tree 未安装时用 find 替代
   find . -maxdepth 2 -type d | sort
   ```

3. **需要观察的现象**：
   - 一级目录与 4.2.2 节的树完全一致；
   - `src/` 下确实是 `ops-nn`、`ops-transformer`、`tests`、`utils` 四项；
   - 注意 `find` 输出中**没有** `output/` 目录——它是执行 `build.sh` 之后才会生成的产物目录（README 第 279 行提到编译成功后在 `output` 目录生成 run 包）。

4. **预期结果**：把实际输出整理成 Markdown 树形图，每个一级目录后面用注释写上职责（可对照 4.2.1 的表格自检）。这张树将作为你后续所有讲义的「定位地图」，建议保存。

#### 4.2.5 小练习与答案

**练习 1**：如果要在仓库里新增一个 `attention` 族算子 `ai_infra_my_attn`，它的 AscendC 源码应该放在哪个绝对路径下？

**参考答案**：`inference/ascendc/src/ops-transformer/attention/ai_infra_my_attn/`。依据：attention 族算子统一放在 `src/ops-transformer/attention/` 下，目录名即算子名（可对照现有 `ai_infra_sparse_flash_attention_gqa` 等）。

**练习 2**：`src/tests/ut/framework_normal` 下的 `op_api`、`op_host`、`op_kernel` 三个子目录暗示了什么？

**参考答案**：暗示每个 AscendC 算子内部由 op_api（API 层）、op_host（算子信息库/Tiling/InferShape 层）、op_kernel（Kernel 层）三层构成，测试框架按同样的层次组织测试支持代码。这与算子目录内的 `op_api/ op_host/ op_kernel/` 子目录一一呼应（见 ascendc README L32–L34）。

**练习 3**：为什么 `cmake/`、`scripts/` 和 `build.sh` 要分开放，而不是全部塞进一个构建目录？

**参考答案**：职责分离——`build.sh` 是用户交互入口（解析命令行参数），`CMakeLists.txt` 是 CMake 的顶层配置，`cmake/` 存放可复用的 CMake 模块与脚本（如 `tiling_sink.cmake`、`ut.cmake`），`scripts/` 存放构建之外的部署辅助脚本。这种分层让构建逻辑可被多个目标（不同 SOC 版本、UT 目标）复用，也让「改一个编译开关」能快速定位到具体文件。

### 4.3 算子列表：六大算子族全景

#### 4.3.1 概念说明

仓库把推理算子按「计算功能」分成六族。理解每族解决什么问题，比记住每个算子更重要：

| 算子族 | 位置 | 解决的问题 | 代表算子 |
|--------|------|-----------|----------|
| Attention | `src/ops-transformer/attention/` | 推理期注意力计算：稀疏注意力、Attention Sink、因果卷积、线性注意力递推等 | `ai_infra_fused_infer_attention_sink` |
| MHC | `src/ops-transformer/mhc/` | Manifold Constrained Hyper Connection（流形约束超连接）结构的前后处理 | `ai_infra_mhc_pre_split_post_res` |
| Index | `src/ops-transformer/index/` | 索引/散射更新 | `ai_infra_scatter_block_update` |
| PosEmbedding | `src/ops-transformer/posembedding/` | 位置编码：RMSNorm + RoPE 后写入 KV Cache | `ai_infra_kv_rms_norm_rope_cache` |
| MoE | `src/ops-transformer/moe/` | 混合专家路由（experts 初始化排序） | `ai_infra_moe_init_routing_v3` |
| Matmul（ops-nn） | `src/ops-nn/matmul/` | 高性能矩阵乘 | `ai_infra_matmul` |

> ⚠️ **重要发现：README 与实际目录不一致，且是双向不一致！**
>
> 根 README 的算子列表（写于仓库早期）**漏掉了** 4 个实际存在的算子目录：
> 1. `src/ops-transformer/moe/ai_infra_moe_init_routing_v3`（整个 moe 族都没出现在 README 的推理算子表里）
> 2. `src/ops-transformer/posembedding/ai_infra_rotary_position_embedding`（旋转位置编码）
> 3. `src/ops-transformer/attention/ai_infra_sparse_flash_attn_metadata`（稀疏注意力元数据）
> 4. `src/ops-nn/matmul/ai_infra_matmul`（ops-nn 整个目录未出现在 README 的推理目录树中）
>
> 反过来，README 收录的 `ai_infra_causal_conv1d_add` 在当前 HEAD（`c1d24e3`）的 `src/ops-transformer/attention/` 下**并不存在**（已用 `ls` 核实）。
>
> 结论：**盘点算子清单时永远以 `ls` 实际目录为准**，README 既可能滞后也可能超前于代码。这一现象在快速迭代的开源仓库中非常普遍，是源码阅读者的基本素养。

#### 4.3.2 核心流程

用一张「文档记载 vs 实际存在」对照表盘点推理算子全景（✓ 表示 README 已收录且目录存在，➕ 表示实际存在但 README 未收录，➖ 表示 README 已收录但当前 HEAD 目录中不存在——均以当前 HEAD `c1d24e3` 的 `ls` 结果核实）：

| 族 | 算子目录 | 说明 | 来源 |
|----|----------|------|------|
| Attention | ai_infra_sparse_flash_attention_gqa | 稀疏 Flash Attention (GQA) | ✓ |
| Attention | ai_infra_sparse_flash_attention_pioneer | 稀疏 Flash Attention (Pioneer) | ✓ |
| Attention | ai_infra_kv_quant_sparse_flash_attention | KV 量化稀疏 Flash Attention | ✓ |
| Attention | ai_infra_fused_infer_attention_sink | 融合推理 Attention Sink | ✓ |
| Attention | ai_infra_fused_infer_attention_sink_metadata | Attention Sink 元数据（AICPU） | ✓ |
| Attention | ai_infra_fused_causal_conv1d | 融合因果一维卷积 | ✓ |
| Attention | ai_infra_causal_conv1d_add | 因果一维卷积加法 | ➖（README 有，目录无） |
| Attention | ai_infra_chunk_gated_delta_rule_recurrence | 分块门控 Delta Rule 递推 | ✓ |
| Attention | ai_infra_esa_select_topk | ESA TopK 选择 | ✓ |
| Attention | ai_infra_lower_triangular_inverse | 下三角矩阵求逆 | ✓ |
| Attention | ai_infra_quant_lightning_indexer | 量化 Lightning Indexer | ✓ |
| Attention | ai_infra_sparse_flash_attn_metadata | 稀疏注意力元数据 | ➕ |
| MHC | ai_infra_mhc_pre_split_post_res | MHC Pre Split Post Res | ✓ |
| MHC | ai_infra_mhc_sandwich_norm_post_preonly | MHC Sandwich Norm Post | ✓ |
| Index | ai_infra_scatter_block_update | Scatter Block Update | ✓ |
| PosEmbedding | ai_infra_kv_rms_norm_rope_cache | KV RMSNorm RoPE Cache | ✓ |
| PosEmbedding | ai_infra_rotary_position_embedding | 旋转位置编码 | ➕ |
| MoE | ai_infra_moe_init_routing_v3 | MoE InitRouting V3 | ➕ |
| Matmul | ai_infra_matmul（ops-nn） | AiInfraMatmul | ➕ |

统计口径（当前 HEAD `c1d24e3`）：`src/ops-transformer` 五族共 17 个实际存在的算子目录（attention 11 + mhc 2 + index 1 + posembedding 2 + moe 1），加 `ops-nn` 1 个，共 **18 个**算子目录（不含各 `common/` 公共目录）。

#### 4.3.3 源码精读

根 README 的推理算子表（注意它只列了 14 行）：

> [README.md:L84-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L84-L101)
>
> ```markdown
> ### 推理算子 (inference/ascendc)
>
> | 分类 | 算子名 | 说明 |
> |------|--------|------|
> | Attention | ai_infra_sparse_flash_attention_gqa | 稀疏 Flash Attention (GQA) |
> | Attention | ai_infra_sparse_flash_attention_pioneer | 稀疏 Flash Attention (Pioneer) |
> | Attention | ai_infra_kv_quant_sparse_flash_attention | KV 量化稀疏 Flash Attention |
> | Attention | ai_infra_fused_infer_attention_sink | 融合推理 Attention Sink |
> | Attention | ai_infra_fused_causal_conv1d | 融合因果一维卷积 |
> | Attention | ai_infra_causal_conv1d_add | 因果一维卷积加法 |
> | Attention | ai_infra_chunk_gated_delta_rule_recurrence | 分块门控 Delta Rule 递推 |
> | Attention | ai_infra_esa_select_topk | ESA TopK 选择 |
> | Attention | ai_infra_lower_triangular_inverse | 下三角矩阵求逆 |
> | Attention | ai_infra_quant_lightning_indexer | 量化 Lightning Indexer |
> | MHC | ai_infra_mhc_pre_split_post_res | MHC Pre Split Post Res |
> | MHC | ai_infra_mhc_sandwich_norm_post_preonly | MHC Sandwich Norm Post |
> | Index | ai_infra_scatter_block_update | Scatter Block Update |
> | PosEmbedding | ai_infra_kv_rms_norm_rope_cache | KV RMSNorm RoPE Cache |
> ```

推理子仓 README 的概述则按算子类别给出了一句话画像：

> [inference/ascendc/README.md:L1-L3](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L1-L3)
>
> ```markdown
> ## 概述
>
> 此项目是基于昇腾的融合推理算子库，当前项目中包括 Attention 类算子（SparseFlashAttention、FusedInferAttentionSink、ChunkGatedDeltaRuleRecurrence 等）、MHC（Manifold Constrained Hyper Connection）类算子、Index 类算子以及 PosEmbedding 位置编码类算子。
> ```

最后补上 4.1 埋下的伏笔——`torch_npu.npu_xxx` 这种写法是怎么生效的。看 `__init__.py` 的挂载逻辑：

> [inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L30-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L30-L47)
>
> ```python
> # get torch.ops.custom module
> custom_ops_module = getattr(torch.ops, 'custom', None)
>
> if custom_ops_module is not None:
>     for op_name in dir(custom_ops_module):
>         if op_name.startswith('_'):
>             # skip built-in method, such as __name__, __doc__
>             continue
>
>         # get custom ops and set to torch_npu
>         custom_op_func = getattr(custom_ops_module, op_name)
>         setattr(torch_npu, op_name, custom_op_func)
>
> else:
>     WARN_MSG = "torch.ops.custom module is not found, mount custom ops to torch_npu failed." \
>                "Calling by torch_npu.xxx for custom ops is unsupported, please use torch.ops.custom.xxx."
>     warnings.warn(WARN_MSG)
> ```

逐行解读：

1. 第 20 行（`from . import custom_ops_lib`，见 [__init__.py:L20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L20)）先导入编译好的 C++ 扩展 `custom_ops_lib`——它通过 `TORCH_LIBRARY` 把所有算子注册进 `torch.ops.custom` 命名空间（注册细节是第 3 单元第 1 讲的主题）。
2. 第 31–41 行：遍历 `torch.ops.custom` 命名空间里的所有算子名，跳过 `_` 开头的内建属性，把每个算子函数 `setattr` 到 `torch_npu` 模块上。
3. 第 43–47 行：如果找不到 `custom` 命名空间（说明 so 未正确加载），打印警告并降级为「只支持 `torch.ops.custom.xxx` 写法」。

这就是「一个函数对象，两种调用路径」的全部秘密——没有魔法，只是运行时的属性拷贝。

#### 4.3.4 代码实践

**实践三：定位六大算子族的代表算子目录并记录绝对路径（源码阅读型实践）**

1. **实践目标**：为六个算子族各选一个代表算子，记录其绝对路径与目录内的一级子目录，形成你的「算子速查表」。

2. **操作步骤**：
   ```bash
   cd inference/ascendc/src

   # 逐族列出算子目录
   ls ops-transformer/attention/
   ls ops-transformer/mhc/
   ls ops-transformer/index/
   ls ops-transformer/posembedding/
   ls ops-transformer/moe/
   ls ops-nn/matmul/

   # 选定代表算子后，查看其内部结构（以最小的 scatter_block_update 为例）
   ls ops-transformer/index/ai_infra_scatter_block_update/
   # 预期看到：CMakeLists.txt  docs  op_api  op_host  op_kernel  tests
   # （个别算子还会有 example/ 或 config.ini 等额外目录，属正常差异）

   # 取绝对路径
   realpath ops-transformer/index/ai_infra_scatter_block_update
   ```

3. **需要观察的现象**：
   - 六个族的目录都与 4.3.2 对照表的「来源」列一致；
   - 任选一个算子目录，内部都能看到 `op_api / op_host / op_kernel / docs / tests` 的固定五件套（AICPU 算子 `ai_infra_fused_infer_attention_sink_metadata` 除外，它是 `op_kernel_aicpu` + `op_graph`）；
   - `realpath` 输出即你要记录的绝对路径。

4. **预期结果**：得到一张六行表格，形如：

   | 族 | 代表算子 | 绝对路径（示例格式） |
   |----|----------|----------------------|
   | Attention | ai_infra_scatter_block_update 的邻居任选，如 `ai_infra_lower_triangular_inverse` | `<repo>/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse` |
   | MHC | `ai_infra_mhc_pre_split_post_res` | `<repo>/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res` |
   | Index | `ai_infra_scatter_block_update` | `<repo>/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update` |
   | PosEmbedding | `ai_infra_kv_rms_norm_rope_cache` | `<repo>/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache` |
   | MoE | `ai_infra_moe_init_routing_v3` | `<repo>/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3` |
   | Matmul | `ai_infra_matmul` | `<repo>/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul` |

   （`<repo>` 为你本地仓库根目录的绝对路径。）

#### 4.3.5 小练习与答案

**练习 1**：根 README 推理算子表列出了 14 个算子，但实际目录有 18 个。请说出至少两个「README 未收录」的算子。

**参考答案**：`ai_infra_moe_init_routing_v3`（moe 族）、`ai_infra_rotary_position_embedding`（posembedding 族）、`ai_infra_sparse_flash_attn_metadata`（attention 族）、`ai_infra_matmul`（ops-nn 族）任答两个即可。验证方法：`ls src/ops-transformer/moe src/ops-nn/matmul` 后与 [README.md:L84-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/README.md#L84-L101) 的表格对比。

**练习 2**：`torch_npu.npu_xxx(...)` 与 `torch.ops.custom.npu_xxx(...)` 是否可能调用到不同的实现？

**参考答案**：不会（在 `__init__.py` 挂载成功的前提下）。`__init__.py` 第 40–41 行把 `torch.ops.custom` 命名空间里的函数对象直接 `setattr` 到 `torch_npu` 上，两个名字引用同一个 Python 函数对象。只有当 `custom_ops_lib` 加载失败（找不到 `torch.ops.custom`）时，`torch_npu.xxx` 写法才不可用，此时代码会发出警告并要求使用 `torch.ops.custom.xxx`。

**练习 3**：算子 `ai_infra_fused_infer_attention_sink_metadata` 的目录里为什么没有 `op_kernel/`？它的 kernel 在哪？

**参考答案**：因为它是 **AICPU 算子**，不跑在 AICore 上，所以目录用的是 `op_kernel_aicpu/`（AICPU Kernel 实现）并额外多了 `op_graph/`（图算子实现），见 [inference/ascendc/README.md:L54-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L54-L60)。它为主算子 `ai_infra_fused_infer_attention_sink` 准备元数据，是「目录结构反映执行介质与角色」的例子。

## 5. 综合实践

**任务：为团队新同事制作一页《openPangu-2.0-Op 推理子仓导览卡》**

把本讲三个模块的输出整合成一份 Markdown 文档（建议 100 行以内），必须包含以下四部分：

1. **一句话定位**：用你自己的语言（不要照抄 README）写清楚这个仓库是干什么的，以及「run 包 + wheel 包」双包协作的关系。
2. **目录树**：`inference/ascendc` 两级目录树，每个一级目录带一句职责注释（来自实践二）。
3. **算子族速查表**：六族代表算子的绝对路径表（来自实践三），并特别标注「README 未收录的 4 个算子」，提醒读者以实际目录为准。
4. **调用方式速记**：抄录 `__init__.py` 文档字符串中的两种调用写法，并用一句话解释 `torch_npu.npu_xxx` 为何可用（setattr 挂载）。

**验收标准**：

- 把导览卡交给一位没接触过本仓库的同事，TA 能在 5 分钟内回答出：算子源码在哪、PyTorch 侧适配在哪、怎么调用、一共几族算子。
- 导览卡中所有路径都经过 `ls` 实际核实，没有凭 README 想象出来的路径。

本实践无需 NPU 硬件，全程只需 `ls`、`realpath`、`head` 等只读命令，可在任何 checkout 了本仓库的机器上完成。

## 6. 本讲小结

- openPangu-2.0-Op 是昇腾亲和大模型算子仓库，顶层分 `training/` 与 `inference/`；**推理子仓（本手册范围）只有 AscendC 一种实现**，Triton/PyPTO 仅存在于训练场景。
- 推理子仓由两大块组成：`src` 下的 **AscendC 算子库**（编译成 CANN run 包，安装进 `opp/vendors`）和 `torch_ops_extension`（打成 `omni_custom_ops` wheel 包，提供 Python 接口）——「发动机」与「方向盘」。
- `inference/ascendc` 一级目录分三组：构建系统（`build.sh`/`CMakeLists.txt`/`cmake`/`scripts`）、算子源码（`src`）、PyTorch 扩展（`torch_ops_extension`）。
- 算子分布在六个族：attention（11 个）、mhc（2 个）、index（1 个）、posembedding（2 个）、moe（1 个）位于 `src/ops-transformer`，matmul（1 个）位于 `src/ops-nn`，共 18 个算子目录。
- **README 的算子表与实际目录双向不一致**：README 漏收 moe、rotary_position_embedding、sparse_flash_attn_metadata、ai_infra_matmul 共 4 项，且收录了并不存在的 `ai_infra_causal_conv1d_add`——盘点以 `ls` 为准，这是读真实仓库的基本素养。
- 每个算子目录呈 `docs / op_api / op_host / op_kernel / tests` 五件套固定形态（AICPU 算子用 `op_kernel_aicpu` + `op_graph` 替代 `op_kernel`），这个形状是下一讲解剖算子目录的地图。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《环境准备与编译安装全流程》。建议先通读 [inference/ascendc/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md) 的「环境准备」与「编译执行」两节（L191–L301），对 Docker 镜像和 `build.sh -c/-n` 参数有个印象，再带着「build.sh 如何把参数变成 CMake 变量」的问题听讲。
- **备选路线（u1-l3）**：如果想先看代码再碰构建，可以直接跳到《解剖一个算子目录：以 ScatterBlockUpdate 为例》，以最小的 `ai_infra_scatter_block_update` 为标本认识五件套内部。
- **延伸阅读**：昇腾社区 Ascend C 自定义算子开发官方资料（ascendc README 第 188 行给出的链接），适合在本手册第 2 单元进入 op_host/op_kernel 之前泛读一遍建立术语体系。
