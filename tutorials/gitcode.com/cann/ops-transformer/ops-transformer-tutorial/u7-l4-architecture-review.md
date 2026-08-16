# 架构复盘与学习路线总结

## 1. 本讲目标

本讲是整个学习手册的收官。前面 26 讲我们从「项目是什么」一路走到「怎么贡献一个算子」，本讲把散落在各讲的知识拉回架构高度，完成三件事：

1. 能从架构图出发，解释 ops-transformer 各层库（op_host / op_api / op_graph / op_kernel / common）之间的依赖关系，以及它们与 CANN 底座（nnopbase、runtime、HCCL）的边界。
2. 能归纳贯穿全仓库的三大横切机制——tiling 路由、多 SoC 架构隔离、多版本并存演进——各自解决什么问题、付出了什么代价。
3. 形成对「同域算子家族 + 版本演进 + 架构隔离」三种复用手段优劣的独立判断，并能提出有源码证据支撑的改进方向。

学完本讲，你应该不再只是「会读这个仓库」，而是能回答「如果让我重新设计一个算子库，我会保留什么、改掉什么」。

## 2. 前置知识

本讲不再引入新的源码细节，而是复用前面各讲已建立的概念。开讲前请确认你理解以下术语：

- **五层算子范式**：单个算子目录由 op_host（Host 侧信息库/tiling/infershape）、op_api（aclnn 两段式 Eager 接口）、op_graph（图模式 proto/graph_infer/fusion_pass）、op_kernel（Device 侧 Ascend C 核函数）、tests/examples 组成（u1-l2、u2-l1）。
- **aclnn 两阶段 API**：GetWorkspaceSize + Run 的调用模型，第一段集中做校验/infershape/tiling，第二段只异步下发（u2-l4、u3-l1）。
- **tiling key / tiling data**：host 填、device 读的「执行计划」，tiling key 是运行期路由到编译期 kernel 变体的整数（u2-l2、u2-l3）。
- **arch 目录**：op_host / op_kernel 下按硬件代际隔离实现的子目录（arch22 对应 ascend910b/910_93，arch35 对应 ascend950，arch38 对应 mc62）（u4-l3）。
- **l0 / L2 分层**：op_api 层内部再分两层——l0 是 base 实现（如 `l0op::FlashAttentionScore`），L2 是面向用户的 aclnn 入口，多个入口可共用一个 base（u4-l2）。
- **fallback 机制**：图模式下经 `OpExecuteFunc` 注册 host 执行函数，运行期转调 aclnn 实现，实现「一份 eager 代码、两种调用路径」（u3-l2）。

如果对以上任何一条感到陌生，建议先回看对应讲义再读本讲。

## 3. 本讲源码地图

本讲引用的关键文件如下（行号均对应当前 HEAD `b2adacfe`）：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `README.md` | 项目总介绍与架构图 | 算子库在 CANN 体系中的位置 |
| `docs/zh/install/dir_structure.md` | 目录层级权威说明 | 「缺层语义」与可选拓扑 |
| `CMakeLists.txt` | 工程编译入口 | 裁剪变量、SoC→arch 映射、模块组装 |
| `build.sh` | 构建脚本唯一入口 | 版本自动补编、模块连带、SoC 白名单 |
| `cmake/func.cmake` | CMake 公共函数 | `op_add_depend_directory` 依赖补编 |
| `moe/moe_token_permute/op_host/CMakeLists.txt` | MoE 重排算子 host 构建配置 | 算子间依赖声明样本 |
| `moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute.cpp` | MoE 重排算子 aclnn 实现 | 跨算子转发样本 |
| `mc2/matmul_all_reduce/op_api/` 目录 | 通算融合算子接口层 | 多版本并存样本 |

## 4. 核心概念与源码讲解

### 4.1 整体架构：分层依赖图

#### 4.1.1 概念说明

复盘一个系统，最有效的入手点是回答两个问题：**它依赖谁**，以及**谁依赖它**。

ops-transformer 在 CANN 体系中的定位，README 用一句话和一张架构图说清：它是 CANN 算子库中「提供 transformer 类大模型计算的进阶算子库」，夹在框架层（PyTorch/torchair/ONNX）与 CANN 底座（nnopbase、runtime、HCCL、opc 编译器）之间。见 [README.md:25-29](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L25-L29)——这段概述明确指出算子库「包括 attention 类、moe 类、mc2 类等」，架构图（`docs/zh/figures/architecture.png`）则画出了它承上启下的位置。

把仓库内部结构抽象成依赖图，可以总结为：

```text
            框架层调用方
   PyTorch(torch_extension)   GE 图引擎   ONNX(ATC 插件)
        │                        │             │
        ▼                        ▼             ▼
   op_api(aclnn 两段式)      op_graph(proto/fusion)   ← 两条调用路径，互不依赖
        │                        │
        └──────────┬─────────────┘
                   ▼
              op_host（def 注册 / infershape / tiling）   ← 两条路径的共同地基
                   │  tiling data / tiling key
                   ▼
              op_kernel（Ascend C 核函数，arch22/arch35/arch38 隔离）
                   │
                   ▼
        CANN 底座：opc 二进制编译 / runtime / HCCL(mc2) / nnopbase(l0 inner)

   common 库：横切所有层，以 OBJECT 库形式被各算子 .so 吸纳
```

这张图有三条关键依赖规则，全部在前面的讲义里出现过，这里合并成一张视图：

1. **op_host 是地基**：Eager（op_api）与 Graph（op_graph）两条路径都依赖它，但彼此互不依赖（u6-l2 的层边界结论）。
2. **op_kernel 只有一份**：无论从哪条路径进来，最终执行的是同一份 Device 侧核函数（u2-l4）。
3. **common 横切所有层**：以 OBJECT 库形式编进各算子动态库，复用零成本（u3-l2）。

#### 4.1.2 核心流程

构建系统如何把这张逻辑依赖图变成物理产物？流程是：

1. `build.sh` 解析命令行选项，白名单校验后翻译成 `-D` cmake 参数（u1-l4）。
2. 顶层 `CMakeLists.txt` 用三个缓存变量驱动裁剪：`ASCEND_COMPUTE_UNIT`（编哪些 SoC）、`ASCEND_OP_NAME`（编哪些算子）、`ASCEND_MODULE_NAME`（编哪些模块）。
3. 按模块 `add_subdirectory`，模块内按算子目录再下钻；算子间依赖由 `op_add_depend_directory` 自动补编。
4. host 源码经 `gen_aclnn_with_opdef()` 从 def 注册自动生成 aclnn 两段式代码骨架，算子作者无需手写（u6-l1）。
5. 每个 SoC 调 `add_bin_compile_target` 做 opc 二进制编译，产出 `.run` 安装包。

#### 4.1.3 源码精读

**证据一：三个裁剪变量 + SoC/arch 平行列表映射。**
[CMakeLists.txt:51-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L51-L61) 定义了三个 `CACHE STRING` 变量和两份按下标平行的列表：`SOC_VERSION_LIST`（ascend310p ascend910b ascend910_93 ascend950 mc62 kirinx90 kirin9030）与 `ARCH_DIRECTORY_LIST`（arch20 arch22 arch22 arch35 arch38 arch22 arch22）。这段代码是「多 SoC 适配」在构建侧的单一事实源——910b 和 910_93 共享 arch22，950 独占 arch35。平行列表的写法很朴素（用下标对齐而非字典），这是 CMake 旧版本兼容下的常见取舍。

**证据二：不支持芯片的空包兜底。**
[CMakeLists.txt:106-119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L106-L119) 对 `ASCEND_COMPUTE_UNIT` 里的每个 SoC 查下标，查不到时打印 `unsupported chip type` 并调用 `cpack_empty_package()` 产出一个空包后 `return()`。这意味着「编一个不支持的芯片」不会报错中断，而是优雅降级为空产物——CI 矩阵可以统一入口而不必为每种芯片写特判。

**证据三：模块组装与「手工追加」的特例。**
[CMakeLists.txt:328-392](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L328-L392) 先 `add_subdirectory(common)`，再按 `should_add_module` 逐模块下钻。注意 [CMakeLists.txt:363-370](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L363-L370) 这段：启用 moe 模块后，手工 `list(APPEND OP_LIST "moe_init_routing_v2")` 把两个不在常规遍历路径上的算子追加进编译清单。这是一个架构信号——通用的自动发现机制覆盖不到的场景，仓库选择在顶层打补丁，而不是改造发现机制本身（后面 4.3 会讨论这个取舍）。

**证据四：算子间依赖的自动补编。**
[CMakeLists.txt:394-406](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L394-L406) 调用 `op_add_depend_directory`，其实现见 [cmake/func.cmake:223-255](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake#L223-L255)：遍历待编算子清单，读取每个算子用 `set(<op>_depends ...)` 声明的依赖（若开启 `ENABLE_EXPERIMENTAL` 会自动改查 `experimental/` 前缀路径），把依赖算子目录加入编译列表。一个真实样本在 [moe/moe_token_permute/op_host/CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/CMakeLists.txt#L11-L13)——moe_token_permute 声明依赖 `moe/moe_init_routing_v2` 和 `moe/moe_init_routing_v3`，因为它的 aclnn 实现转发这两个算子的 l0 接口（见 4.3.3）。

**证据五：缺层语义——目录拓扑是接口说明书。**
[dir_structure.md:5-10](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L5-L10) 官方定义了「缺层」的含义：缺 op_host 或 op_kernel 可能是复用了其他算子实现，缺 op_api 说明不支持 aclnn 调用，缺 op_graph 说明不支持图模式。这是本仓库最重要的架构约定之一——**目录结构本身就是算子能力的自描述**，读者不需要跑代码就能判断一个算子的交付形态。配套的五层目录明细见 [dir_structure.md:39-65](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L39-L65)，其中还标注了各文件的「可选」属性（如 `${op_name}_infershape.cpp` 可选、`op_api` 目录可选）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「目录结构即接口说明书」这条架构约定，把本节讲的依赖图落到具体算子上。

**操作步骤**：

1. 任选三个算子目录（建议：`attention/flash_attention_score`、`mc2/moe_distribute_combine`、`moe/moe_token_permute`），用 `ls` 列出各自的子目录集合。
2. 对照 dir_structure.md 的缺层语义，逐个回答：该算子支持 aclnn 调用吗？支持图模式吗？是自带 kernel 还是复用别人实现？
3. 对 `moe_token_permute` 特别验证：它的顶层没有 `op_api` 目录，但 aclnn 文件藏在 `op_host/op_api/` 下——用 `find moe/moe_token_permute -name "aclnn_*"` 确认。
4. 画出这三个算子各自的「五层覆盖图」（哪些层有、哪些层缺、缺的层去哪了）。

**需要观察的现象**：三个算子的目录形态并不相同——flash_attention_score 五层俱全；moe_token_permute 的 aclnn 层挪进了 op_host；你选的第三个算子可能又是另一种形态。

**预期结果**：你会发现「五层范式」是约定而非强制，仓库存在至少三种合法变体（标准五层 / aclnn 内嵌 op_host / 转发复用他人实现）。这正是 4.3 节要复盘的「灵活性 vs 一致性」取舍的实证。本实践为纯源码阅读，不依赖 NPU 环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `common` 库要在 `CMakeLists.txt:328` 第一个被 `add_subdirectory`，而不是和算子域模块并列？

**参考答案**：common 是横切所有层的公共代码，以 OBJECT 库形式被 mc2/attention/moe 等各算子动态库吸纳（u3-l2）。先加入构建树才能保证后续所有算子 target 引用它时目标已存在、头文件路径已就绪。这也体现了它在依赖图中的位置：不依赖任何算子域，被所有算子域依赖。

**练习 2**：一个算子目录缺 `op_api`，官方文档给出两种可能解释，请分别举一个应对动作。

**参考答案**：解释一是「不支持 aclnn 调用」（此时用户只能走图模式，或等社区补充）；解释二是「调用了其他算子 op_api 实现，调用逻辑在 op_host 或 op_graph 下」（此时应顺着源码找转发目标，例如 moe_token_permute 在 `op_host/op_api/` 下转发 `l0op::MoeInitRoutingV2/V3`）。应对动作：先查该算子的 README 产品支持表确认交付范围，再 `find <op_dir> -name "aclnn_*"` 搜索实现文件的实际位置。

### 4.2 演进：三大横切机制的设计模式

#### 4.2.1 概念说明

如果说「分层」是仓库的纵向骨架，那么有三条机制是横向切开所有算子的，我们称之为**横切机制（cross-cutting concern）**——任何算子都逃不开它们，但它们不属于任何单一算子：

1. **tiling 路由**：同一算子面对不同 shape/dtype/场景时，如何选出并路由到正确的 kernel 变体。
2. **多 SoC 架构隔离**：同一算子面对不同硬件代际时，如何在一份代码库里共存多套实现。
3. **多版本并存演进**：同一算子的 V1/V2/V3 接口如何共存、如何复用、如何过渡。

这三条机制的共同设计模式可以概括为一句话：**「键 + 注册表 + 路由」**——用一个键（tiling key / SoC 名 / 版本号）在编译期或运行期查表，路由到具体实现，而不是把所有分支写进同一个函数。

#### 4.2.2 核心流程

三条机制的路由时机不同，这是理解它们差异的关键：

| 机制 | 键 | 路由时机 | 注册表 |
| --- | --- | --- | --- |
| tiling 路由 | tiling key（整数，按 dtype/场景特征位编码） | **运行期** host 算 key，选到**编译期**已生成的 kernel 二进制变体 | binary.json 变体表（u2-l1） |
| 多 SoC 隔离 | SoC 名（ascend910b → arch22...） | **编译期**按 `--soc` 参数只编对应 arch 目录 | SOC_VERSION_LIST / ARCH_DIRECTORY_LIST 平行列表 |
| 多版本演进 | 版本号（V2/V3） | **编译期**入口选择 + **运行期**按 SoC 能力转发到 inner | 多个 aclnn 入口文件 + 唯一 base/inner 实现 |

#### 4.2.3 源码精读

**证据一：tiling sink 的按需裁剪——横切机制也要付编译成本。**
[build.sh:1343-1356](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1343-L1356)：当用户用 `--ops` 指定算子编译时，脚本检查算子名单——若不含 `fused_infer_attention_score` 和 `incre_flash_attention`，就追加 `-DENABLE_TILING_SINK=OFF`。tiling sink（tiling 结果下沉缓存）是 CMakeLists 里的默认开启选项（`CMakeLists.txt:33` 的 `option(ENABLE_TILING_SINK ... ON)`），但绝大多数算子用不上它，只有两个推理侧 attention 算子受益。这条代码揭示了横切机制的隐藏成本：**即使只有 2 个算子需要，机制本身也得全局默认开启，只能在窄化编译时手工关掉**。

**证据二：SoC 白名单的单一数据源。**
[build.sh:50-53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L50-L53) 定义了 `SUPPORT_COMPUTE_UNIT_SHORT` 数组，注释写明它的来源：「取自 cmake/scripts/util/const_var.py 的 SOC_MAP_EXT keys + CMakeLists.txt SOC_VERSION_LIST 中的 mc62」。也就是说，SoC 支持列表事实上维护在 build.sh、CMakeLists.txt、const_var.py 三处，靠注释人工保持同步——这是「平行列表」方案的直接代价（4.3 再展开）。

**证据三：版本自动补编——多版本并存的构建侧补丁。**
[build.sh:1344-1352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1344-L1352)：用户只指定 `moe_distribute_combine_v2` 时，脚本自动追加 `moe_distribute_combine_v3`；dispatch 同理；`distribute_barrier` 自动补编 `distribute_barrier_extend`。这是因为 u5-l4 讲过的架构事实：950 上 v2 的 base 层经 `Mc2Context` 路由到 v3 的 inner 实现——**只编 v2 不编 v3 会产生悬空符号**。注意这里的补编逻辑写在 build.sh（shell 字符串匹配），而依赖补编机制 `op_add_depend_directory` 写在 cmake——同一类问题用了两套工具解决。

**证据四：模块级连带。**
[build.sh:1359-1364](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1359-L1364)：指定 `--module=mc2` 时自动连带 `gmm` 模块。mc2 的通算融合算子依赖 gmm 的 grouped_matmul 系列做底层矩阵乘，模块间存在数据依赖但依赖关系没有声明化，只能靠脚本硬编码。对比 4.1.3 证据四：**算子级依赖已声明化（`_depends` 变量），模块级依赖还是硬编码**——依赖声明机制的覆盖是不完整的。

**证据五：多版本入口的物理形态。**
`mc2/matmul_all_reduce/op_api/` 目录下并列着 10 组 aclnn 入口文件（`aclnn_matmul_all_reduce` 及其 `_v2`/`_v3`、`aclnn_quant_matmul_all_reduce` 的 `_v1`~`_v5`、`aclnn_weight_quant_matmul_all_reduce` 的 `_v1`/`_v2`）加一组共享的 `matmul_all_reduce_util.cpp/h`。u5-l3 已讲过它们的共同去向：全部翻译补空为超集参数后汇聚到唯一的 inner 接口（由 CANN 包 nnopbase 提供）。这个目录是多版本演进最直观的物理呈现——**每个版本一个入口文件，公共逻辑沉到 util/base**。

#### 4.2.4 代码实践

**实践目标**：验证「版本自动补编」机制真实生效，并理解它防止的是什么故障。

**操作步骤**：

1. 在仓库根目录执行 `bash build.sh --ophost --ops=moe_distribute_combine_v2 --noexec`（`--noexec` 只做配置不真正编译；若该选项不可用，直接观察 cmake 配置日志）。
2. 在输出日志中搜索 `ASCEND_OP_NAME`，确认其值是否被脚本扩写为 `moe_distribute_combine_v2;moe_distribute_combine_v3`。
3. 对照 [build.sh:1344-1352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1344-L1352) 阅读字符串匹配逻辑，思考：如果新出一个 `moe_distribute_dispatch_v4` 且 v2 在 950 上也要转发到 v4，这段脚本需要怎么改？
4. 再试 `bash build.sh --ophost --module=mc2 --noexec`，观察日志中模块名是否被扩写为 `mc2,gmm`。

**需要观察的现象**：命令行传入的算子名/模块名与 cmake 日志中实际生效的值不一致——被脚本静默扩写了。

**预期结果**：确认「用户请求的编译单元」≠「实际编译单元」，扩写规则散落在 build.sh 的字符串匹配里。第 3 步的答案应当是：再改一处 if 分支（这正是该设计被批评的点，也是综合实践里「提出改进方向」的素材）。本实践只需 cmake 配置阶段，无需 NPU，编译行为「待本地验证」（取决于环境中 CANN 包是否就绪）。

#### 4.2.5 小练习与答案

**练习 1**：tiling 路由与多 SoC 隔离都是「一份代码多套实现」，本质区别是什么？

**参考答案**：路由时机与键的来源不同。tiling 路由发生在**运行期**，键（tiling key）由 host 侧根据输入 shape/dtype 计算，同一块卡、同一次部署里会动态选择不同 kernel 变体；多 SoC 隔离发生在**编译期**，键（SoC 名）来自用户的 `--soc` 参数，编出的包里只有一份 arch 目录的实现，运行期不再选择。前者解决「同一硬件上的场景多样性」，后者解决「不同硬件代际的指令集差异」。

**练习 2**：为什么 `ENABLE_TILING_SINK` 默认全仓开启、只在窄化编译时关闭，而不是默认关闭、仅在两个受益算子的 CMakeLists 里局部开启？

**参考答案**：因为出整包（不传 `--ops`）时必须让所有算子行为一致，默认值决定的是「整包口径」；若默认关，单独编 FA/FIA 时忘了开就会得到与整包不同的二进制，破坏可复现性。这是一个「默认值取超集保证一致性，裁剪责任交给窄化场景」的典型权衡。代价是普通算子单独编译时也要显式关掉（由 build.sh 代劳）。

### 4.3 取舍：三种复用手段的优劣与改进方向

#### 4.3.1 概念说明

有了分层骨架（4.1）和横切机制（4.2），最后一层复盘是：仓库靠什么控制代码重复、支撑数百个算子共存？归纳起来是三种复用手段，对应规格中三个代表算子：

1. **同域算子家族**（代表：flash_attention_score 家族）——同一业务域内的算子共享目录范式、common 工具与 base 实现，新算子是「家族新成员」而不是孤岛。
2. **版本演进**（代表：matmul_all_reduce）——同一下游需求变化时，新增版本入口、汇聚到唯一 inner，老版本垫片长期保留。
3. **架构隔离**（代表：moe_token_permute）——跨代硬件或跨算子复用时，用转发（l0op 调用）+ 依赖补编把差异关进局部。

三种手段不是互斥的，而是三个正交的轴：家族管「横向同类」，版本管「纵向时间」，架构管「深度分层」。

#### 4.3.2 核心流程

评估一种复用手段，用三个维度打分：

- **新增成本**：接一个新成员（新算子/新版本/新 SoC）要写多少代码、改几处公共设施。
- **理解成本**：读者定位一个行为的最终实现要跳几次文件。
- **演进风险**：改动公共部分时，波及面是否可控、是否有护栏。

用统一尺度衡量（★ 多为优）：

| 手段 | 新增成本 | 理解成本 | 演进风险 | 主要证据 |
| --- | --- | --- | --- | --- |
| 同域家族 | 低（拷范式 + 复用 common） | 中（家族越大越难穷举） | 中（common 是单点） | attention 域 60+ 算子同范式 |
| 版本演进 | 低（加一个垫片入口） | 高（跳 2～3 层才到实现） | 低（老入口不动，天然兼容） | matmul_all_reduce 10 入口 1 inner |
| 架构隔离 | 高（要建转发 + 声明依赖） | 中（转发路径显式可追） | 中（补编规则散在脚本） | moe_token_permute 转发 v2/v3 |

#### 4.3.3 源码精读

**证据一：架构隔离——运行期按平台能力转发。**
[moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute.cpp:205-236](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute.cpp#L205-L236)：实现里用 `Ops::Transformer::AclnnUtil::IsRegbase()` 判断平台是否走 regbase 路径，不是则 `BuildMoeInitRoutingV3Executor`（该函数内部调 `l0op::MoeInitRoutingV3`，见同文件 152 行），是则走 `l0op::MoeInitRoutingV2`。也就是说 Ascend 950 上 MoeTokenPermute 是个纯兼容壳，真实计算在 moe_init_routing_v2/v3 里。配合 [moe/moe_token_permute/op_host/CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/CMakeLists.txt#L11-L13) 的 `set(moe_token_permute_depends moe/moe_init_routing_v2 moe/moe_init_routing_v3 ...)`，构成「运行期转发 + 编译期依赖补编」的完整闭环。**优点**：算子语义复用零拷贝；**代价**：读代码的人必须知道「去哪找真实现」，README 的参数映射表成了转发合同（u5-l1）。

**证据二：版本演进——入口爆炸但实现唯一。**
4.2.3 证据五列出的 `mc2/matmul_all_reduce/op_api/` 下 10 组入口文件，全部把参数翻译补空为超集后调唯一 inner。**优点**：任何老版本调用方永不破坏，新版本只写「填空题」（u4-l2 对 FA 家族 13 组 L2 接口共用 31 参数 base 的分析同构）；**代价**：目录里文件数线性膨胀，且「哪个版本推荐用」的信息只能靠 README 维护——版本之间不是代码里可见的继承关系，而是「都汇聚到同一个黑盒 inner」的隐式关系。

**证据三：同域家族——范式一致但存在合法变体。**
attention 域下并列 60+ 个算子目录（flash_attention_score、flash_attn、quant_flash_attn、prompt_flash_attention、incre_flash_attention、lightning_indexer 系列等），全部遵循五层范式，共享 `attention/common` 与顶层 `common`。**优点**：学会一个等于学会全部，这正是本手册能用 add_example 教完全部范式的原因；**代价**：变体没有强制约束（4.1.4 实践中你已发现 aclnn 目录可内嵌 op_host），且家族成员间偶有职责重叠（如 flash_attention_score 与 2026/05 新增的 flash_attn 按「量化/非量化」切分边界，见 [README.md:7](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L7)）。

**证据四：一个可改进点的直接证据——依赖声明的三种机制并存。**
同一个「B 依赖 A，编 B 要带上 A」问题，仓库里有三套解法：(a) cmake 声明式——`op_add_depend_directory` 读 `<op>_depends` 变量（[cmake/func.cmake:223-255](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake#L223-L255)）；(b) 顶层手工追加——`CMakeLists.txt:366-369` 对 moe_init_routing_v2 的 `list(APPEND)`；(c) build.sh 字符串匹配——`build.sh:1344-1352` 对 dispatch/combine v3 的补编。三套机制语义重叠、位置分散，新人无法从单一入口获知某算子的全部编译依赖。

#### 4.3.4 代码实践

**实践目标**：完成规格要求的架构复盘作业——归纳三种复用手段优劣，提出 2 条改进方向并附源码证据。

**操作步骤**：

1. **选定分析对象**：flash_attention_score（家族）、matmul_all_reduce（版本）、moe_token_permute（隔离），每个算子各找到一条「复用发生」的具体代码路径：
   - flash_attention_score：从任一 L2 入口跳到 `l0op::FlashAttentionScore` base 的调用点（u4-l2 已追踪过 scale_value 路径，直接复用结论）；
   - matmul_all_reduce：任选 `_v2` 入口，找到它补空参数后调 inner 的那一行（u5-l3 的 util 漏斗）；
   - moe_token_permute：[aclnn_moe_token_permute.cpp:205-236](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute.cpp#L205-L236) 的 IsRegbase 分叉。
2. **填评估表**：按 4.3.2 的三维度（新增/理解/演进风险）给三种手段打分，每格写一句证据。
3. **提出改进方向**（至少 2 条），每条必须满足「有源码证据 + 有具体改法」。可直接采用或反驳下面两条候选：
   - **候选 A（统一依赖声明）**：把 `build.sh:1344-1352` 的补编规则和 `CMakeLists.txt:366-369` 的手工追加，统一收敛到 `op_add_depend_directory` 的 `_depends` 声明里（在 moe_distribute_combine_v2 的 op_host/CMakeLists.txt 声明 `moe/moe_distribute_combine_v3`），build.sh 里的字符串匹配即可删除。收益：依赖关系单一入口可查。
   - **候选 B（SoC 注册表字典化）**：把 `CMakeLists.txt:60-61` 的平行列表和 `build.sh:53` 的数组、`const_var.py` 的 SOC_MAP_EXT 收敛为一个生成源（例如由 const_var.py 生成 cmake 片段），三处消费。收益：新增 SoC 只改一处，消除注释里自认的「人工保持同步」风险。
4. **写总结**：把以上内容整理成 800～1200 字的架构复盘笔记，存入你自己的学习目录（不要写入仓库源码目录）。

**需要观察的现象**：填表过程中你会发现「理解成本」最难客观打分——建议用「跳转次数」量化：从用户 API 调用语句到最终执行的 kernel 入口，中间经过几个文件。

**预期结果**：三个算子的跳转次数约为——matmul_all_reduce 3 跳（入口 → util → inner）、moe_token_permute 3 跳（入口 → 转发构造 → l0 实现）、flash_attention_score 2～3 跳（入口 → base）。改进建议是否能成立，取决于你给出的证据链是否完整。本实践为纯源码阅读与写作，无需 NPU 环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么 matmul_all_reduce 选择「每个版本一个入口文件」而不是「一个入口 + 版本参数」？

**参考答案**：aclnn 是 C ABI 风格的稳定接口，已发布的函数签名（参数个数、类型）不能变——老调用方是编译期绑定符号的。加版本参数等于改签名，会破坏所有存量二进制。所以演进只能靠新增符号（新函数名），旧符号永久保留为垫片。这与操作系统的 syscall 编号只增不改是同一类约束。代价是文件数膨胀，由「汇聚到唯一 inner」来控制重复。

**练习 2**：moe_token_permute 的转发模式（复用 moe_init_routing_v2/v3）与直接拷贝一份实现相比，牺牲了什么换来了什么？

**参考答案**：牺牲——读代码多一跳，两个算子的接口参数存在「翻译层」（转发合同靠 README 映射表维护，代码不强制同步），且被复用算子的任何接口变动都可能波及转发方；换来——bug 修复与性能优化在底层算子发生一次即全家受益（u5-l1 提到 950 上 MoeTokenPermute 的能力随 MoeInitRoutingV3 升级而升级），以及 tiling/kernel 不需要为兼容壳各养一份。

**练习 3**：如果让你为本仓库新增一个横切机制（比如「精度等级路由」：同一算子按 fp16/fp8 自动选不同 kernel），应该套用哪条现有机制的模式？为什么？

**参考答案**：套用 tiling 路由的模式——精度本来就是 tiling key 的既有编码维度之一（u2-l2 讲过 add_example 按 dtype 分 tilingKey）。做法是：host 侧 tiling 函数根据输入 dtype 计算含精度特征位的 tiling key，kernel 侧在 binary.json 注册对应变体，运行期自动路由。不需要发明新机制，因为「键 + 注册表 + 路由」的骨架已经存在；真正要做的只是扩展键的编码空间并补齐变体注册。

## 5. 综合实践

综合实践把本讲全部内容串成一份可交付的「架构复盘报告」，也是整个学习手册的毕业作业：

**任务：写一份《ops-transformer 架构评审报告》。**

1. **画依赖图**：以本讲 4.1 的分层依赖图为基础，把你在前六单元精读过的具体算子标注到图上（例如在 op_api 层标 flash_attention_score 与 matmul_all_reduce，在转发路径上标 moe_token_permute → moe_init_routing_v2/v3）。要求每条边都能指出一个源码锚点（文件:行号）。
2. **机制对照**：选一条你最有体感的横切机制（tiling / 多 SoC / 多版本），写 300 字说明它的「键 + 注册表 + 路由」三要素分别落在哪些文件，并指出它的成本支付点（如 tiling sink 的默认开启、SoC 平行列表的三处维护）。
3. **复用手段评估**：完成 4.3.4 的评估表与两条改进方向。
4. **延伸验证（可选，需编译环境）**：用 `bash build.sh --ophost --ops=moe_distribute_dispatch_v2 --noexec` 与 `--ops=moe_token_permute --noexec` 各配置一次，对比日志中被自动扩写的编译单元，验证依赖补编与手工追加两种机制的行为差异。「待本地验证」（依赖环境中 CANN 包就绪）。

完成这份报告后，你可以拿它与仓库 `docs/zh/install/dir_structure.md` 的官方描述对照：哪些架构行为官方文档写明了，哪些只存在于 build.sh 和 CMakeLists 的代码里——这个差集就是「读源码比读文档多知道的 part」，也是你未来给社区提 PR 改进文档的候选项。

## 6. 本讲小结

- ops-transformer 的分层依赖图：op_host 是 Eager（op_api）与 Graph（op_graph）两条调用路径的共同地基，op_kernel 全库只有一份实现，common 以 OBJECT 库横切所有层；目录结构本身即算子能力的自描述（缺层语义见 dir_structure.md:5-10）。
- 三大横切机制共享「键 + 注册表 + 路由」模式，但路由时机不同：tiling 运行期选编译期变体、多 SoC 编译期按 `--soc` 裁剪 arch 目录、多版本靠「多入口垫片 + 唯一 inner/base」实现永不破坏的演进。
- 三种复用手段各有代价：同域家族一致性高但存在合法变体、版本演进兼容性最好但入口文件线性膨胀、架构隔离零拷贝复用但理解成本转移到转发合同上。
- 构建系统是架构的镜像：三个裁剪缓存变量（SoC/算子/模块）、should_add_module 的「不传即全量」、空包兜底、依赖自动补编——但也暴露了平行列表多处维护、依赖声明三机制并存等技术债。
- 复盘的产出不是「背下结论」，而是能对任意一段构建/组织代码问出「它在解决什么问题、付出了什么、还能怎么改」。

## 7. 下一步学习建议

本讲结束意味着手册主体路线（会用 → 会读 → 会写 → 会贡献）闭环完成。继续深入有三个方向：

1. **横向扫域**：用本手册的范式精读方法自学未覆盖的模块——`gmm/`（grouped_matmul 系列，9 个算子，是 mc2 通算融合的底层依赖，可从 `grouped_matmul` 的 def/tiling 入手对照 u5-l3）、`mhc/`（mhc_pre/post/sinkhorn 及其反向，注意它的 `mhc_sinkhorn_common` 是域内公共库的组织样本）、`posembedding/`（15 个 rope/位置编码算子，形态最接近「传统单功能算子」，适合对照 attention 类融合大算子理解粒度差异）、`mamba/`（目前仅 causal_conv1d，可关注其后续演进）。
2. **纵向追底座**：op_api 层大量调用的 `l0op::` 命名空间与 nnopbase 的 inner 接口来自 CANN 安装包（`ASCEND_CANN_PACKAGE_PATH` 指向的 nnopbase 头文件与库），追进去可以理解「进阶算子库」与「基础算子库」的分工边界——建议从 `mc2/matmul_all_reduce/op_api/matmul_all_reduce_util.cpp` 里的 inner 调用点起步（u5-l3 已铺过路）。
3. **参与演进**：把综合实践中发现的文档差集或改进方向落成真实贡献——小到给某个算子 README 补充「该算子转发到 X 实现」的说明，大到按 u7-l3 的流程提一个构建系统优化 PR。仓库名单里 mamba 域只有一个算子、dir_structure.md 承认部分算子缺 Ascend C 实现「欢迎开发者补充贡献」，这些都是新人的切入点。

最后一条建议：每读完一个新算子，回到本讲的 4.3.2 评估表给它归一次类。当你能在十分钟内判断「这个算子属于哪个家族、处于哪个版本阶段、做了哪层隔离」，你就真正拥有了独立阅读这个仓库（以及任何同类算子库）的能力。
