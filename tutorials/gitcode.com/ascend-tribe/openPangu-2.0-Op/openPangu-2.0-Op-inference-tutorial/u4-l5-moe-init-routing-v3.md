# u4-l5 MoE InitRouting V3：多 Tiling 模板与 SOC 配置

## 1. 本讲目标

本讲精读 `ai_infra_moe_init_routing_v3`——MoE（Mixture of Experts，混合专家）路由分发算子。它是仓库中「模式组合爆炸」的典型代表：非量化/静态量化/动态量化 × GATHER/SCATTER 索引 × DropLess/DropPad × 单核/多核排序 × EP 专家子集，这些模式两两组合，催生了 kernel 侧 30 余个 `moe_v3_*` 头文件。

学完本讲，你应该能够：

1. 解释 `op_host/config/<soc>/` 下 `*_binary.json` 与 `*_simplified_key.ini` 两个配置文件各自记录什么、被构建系统的哪个环节消费。
2. 手工演算 host 侧 `GetTilingKey()` 的编码公式，并在 kernel 入口找到对应的 `TILING_KEY_IS` 分支与 kernel 模板类。
3. 说明 drop_pad、量化等模式组合如何映射到具体的 kernel 变体，以及 host 与 kernel 两侧必须镜像一致的「谓词」纪律。

本讲是第 4 单元（核心算子族精读）的一讲，直接建立在 u2-l3（Tiling 七步框架）与 u2-l4（AscendC Kernel 入门）之上；与 u4-l1 的 FIA Sink 对比阅读效果最佳——FIA 用自建 `FiaTilingRegistry` 注册多个 tiling 模板类轮询，而 MoE V3 host 侧只有一个 tiling 模板类，「多变体」全部下沉到 TilingKey 编码与 kernel 模板组合上。

## 2. 前置知识

### 2.1 MoE 路由算子在做什么

MoE 层先用门控网络（如 `npu_moe_gating_top_k_softmax_v2`）为每个 token 选出 top-k 个专家，得到 `expert_idx`（shape 为 `[N, K]`，N 是 token 数，K 是每 token 选中的专家数）。本算子负责随后的「分发准备」：

1. 对 `expert_idx` 全量排序，得到按专家分组后的行顺序；
2. 输出 `expanded_row_idx`：排序后位置与原行号的映射，GATHER 索引（`row_idx_type=0`）或 SCATTER 索引（`row_idx_type=1`）两种语义；
3. 统计每个专家分到的 token 数（count / cumsum / key_value 三种直方图模式）；
4. 可选地按映射搬运 `x` 得到 `expanded_x`，并在搬运途中做 int8 量化（静态量化用输入 scale/offset，动态量化现场求 scale）。

官方文档的接口功能与计算公式见 [docs/npu_ai_infra_moe_init_routing_v2.md:17-20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/docs/npu_ai_infra_moe_init_routing_v2.md#L17-L20) 与 [docs/npu_ai_infra_moe_init_routing_v2.md:32-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/docs/npu_ai_infra_moe_init_routing_v2.md#L32-L47)（本目录 docs 以 v2 命名，v3 是它的量化增强版）。

### 2.2 本讲要用的旧知识

- **TilingKey 双侧契约**（u2-l3/u2-l4）：host 侧算出一个 uint64 的 key 经 `SetTilingKey` 落账，kernel 侧用 `TILING_KEY_IS(宏)` 对号入座；两侧数值必须硬编码一致，否则 kernel 静默空跑。
- **TilingBaseClass 七步框架**（u2-l3）：`GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling`。
- **GET_TILING_DATA / TPipe / TQue**（u2-l4）：kernel 入口解包施工图、UB 内存划拨与队列式搬运。
- **模式字典**：`drop_pad_mode` 0=DropLess（不裁剪）/ 1=DropPad（按 `expert_capacity` 裁剪补齐）；`quant_mode` -1=不量化 / 0=静态量化 / 1=动态量化。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `op_host/ai_infra_moe_init_routing_v3_tiling.h` | TilingData 结构定义：8 个子阶段子结构 + 主结构 + CompileInfo |
| `op_host/ai_infra_moe_init_routing_v3_tiling_base.cpp` | 框架挂接：`IMPL_OP_OPTILING` 注册 tiling 入口与 TilingParse |
| `op_host/ai_infra_moe_init_routing_v3_tiling.cpp` | 主体：参数检查、各子阶段切分、TilingKey 编码、workspace 计算、模板注册 |
| `op_host/config/ascend910_93/moe_init_routing_v3_binary.json` | 910_93 预编译二进制清单（7 条 dtype 组合） |
| `op_host/config/ascend910_93/moe_init_routing_v3_simplified_key.ini` | opc 编译 `--simplified_key_mode` 取值配置 |
| `op_host/config/ascend910b/…` | 同上两件，910b 版本 |
| `op_kernel/ai_infra_moe_init_routing_v3.cpp` | kernel 入口：TilingKey 宏定义 + 多级分发 |
| `op_kernel/moe_v3_common.h` | 公共常量（GATHER/SCATTER、SCALE 模式等）与对齐工具 |
| `op_kernel/moe_v3_sort_base.h` | 排序族 kernel 的公共基类 `MoeSortBase` |
| `op_kernel/moe_v3_full_load.h` | 性能打点专用全载模板 `MoeV3FullLoad`（精读标本） |
| `op_kernel/moe_v3_*.h`（其余 20+ 个） | 按「排序/计数排序/全载/直方图/gather/droppad」组合的 kernel 模板 |
| `tests/ut/op_host/test_ai_infra_moe_init_routing_v3_tiling.cpp` | tiling UT：断言期望 TilingKey 与 workspace |
| `ascendc/CMakeLists.txt`、`ascendc/cmake/func.cmake` | 消费 config 目录、传递 tiling_key 的构建环节 |

## 4. 核心概念与源码讲解

### 4.1 模块一：SOC 配置文件——binary.json 与 simplified_key.ini

#### 4.1.1 概念说明

AscendC 算子编出来的是「一份源码 → 多份二进制」：同一个 kernel 源码要按 SOC 版本（ascend910b / ascend910_93）、输入 dtype 组合分别编译成 `.o` 二进制。为了让编译工具（opc）与运行时框架知道「哪些组合已经有现成二进制、二进制文件叫什么名字」，每个算子可以在 `op_host/config/<soc版本>/` 下放两个清单文件：

- **`<op>_binary.json`**：预编译二进制的「签名卡目录」。每条记录声明一份二进制的文件名（内容哈希命名）、它服务的输入/输出 dtype/format/shape 特征，以及配套的属性默认值。
- **`<op>_simplified_key.ini`**：告诉 opc 工具编译时给 `--simplified_key_mode` 选项传什么值。simplified key 是 CANN 的二进制复用机制——把「运行效果等价的输入签名集合」归并成一个简化 key，让一份二进制服务多种具体 shape，减少变体数量。

这就是「SOC 配置」的含义：**同一份算子源码，按芯片型号放不同的配置目录，编译出该芯片的二进制变体集合**。

#### 4.1.2 核心流程

```text
算子源码 + op_host/config/<soc>/*.json/*.ini
        │
        ├── 构建期: build.sh -c <soc>  → CMake 按 SOC 收集 config
        │     └─ ascendc_bin_param_build.py / opc 按清单生成编译命令
        │        （--tiling-keys / --simplified_key_mode 等选项）
        │     └─ 编译产物 .o + json 安装到
        │        vendors/<vendor>/op_impl/ai_core/tbe/config/<soc>/
        │
        └── 运行期: CANN 框架按算子调用签名查清单，
           命中则直接加载对应 bin_filename 的二进制，免现场编译
```

#### 4.1.3 源码精读

**（1）binary.json 的结构。** 文件顶层是 `op_type` 与 `op_list` 数组，每条记录以哈希命名的 `bin_filename` 开头：

- [moe_init_routing_v3_binary.json:1-5](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_binary.json#L1-L5)：声明算子类型 `MoeInitRoutingV3` 与第一条二进制的文件名 `MoeInitRoutingV3_19802a760bc5088fa9a91fb0460dbe6b2ab7780`——后缀是内容哈希，源码或参数变了哈希就变，避免新旧二进制混用。
- [moe_init_routing_v3_binary.json:6-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_binary.json#L6-L47)：第一条的输入表——`x`(int8)、`expert_idx`(int32) 必选，`scale`/`offset`(float32) 可选（`paramType: optional`），`shape: [-2]` 表示动态形状、任意维度。
- [moe_init_routing_v3_binary.json:90-139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_binary.json#L90-L139)：属性表把 9 个属性全部固定为默认值（`active_num=-1`、`drop_pad_mode=0`、`quant_mode=-1`、`row_idx_type=0` 等）——即预编译二进制面向「默认属性组合」，非默认属性组合需要另外的编译变体。

整个文件共 7 条 `op_list` 记录，按 `x → expanded_x` 的 dtype 组合分两类：

| 条目 | x dtype | expanded_x dtype | 语义 |
|---|---|---|---|
| 1 | int8 | int8 | int8 输入原样透传（不允许量化） |
| 2~4 | fp16 / fp32 / bf16 | 同 x | 非量化原样输出 |
| 5~7 | fp16 / fp32 / bf16 | int8 | 量化输出（expanded_x 变 int8） |

**（2）simplified_key.ini 的自述。** 这个文件最好的文档就是它自己的注释。[moe_init_routing_v3_simplified_key.ini:9-19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_simplified_key.ini#L9-L19) 的注释说明了规则：该文件影响 opc 工具编译二进制 kernel 时 `--simplified_key_mode` 选项的取值；`default` 是默认 mode，`ascendxx=xx` 用于个别芯片有差异化要求时覆盖；不配置时 AscendC 算子按 `simplified_key_mode=0` 处理。[moe_init_routing_v3_simplified_key.ini:20-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_simplified_key.ini#L20-L21) 即本算子的实际取值：`default=0`（用框架默认模式）。`ascend910b` 目录下的两个文件与 910_93 版本内容完全一致（可用 diff 验证）。

**（3）构建系统如何消费。** 本算子目录的 CMakeLists 并不直接引用 config 目录，消费发生在全局构建脚本里：

- [cmake/func.cmake:426-427](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L426-L427)：把生成的 `<op>.json` 安装到 `${_INSTALL_DIR}/config/${BINARY_COMPUTE_UNIT}`——即 run 包里 vendors 目录下按 SOC 分层的 config 目录。
- [cmake/func.cmake:507-509](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L507-L509)：调用 `ascendc_ops_config.py` 汇总生成 `binary_info_config.json`（所有算子二进制信息的总索引），同样安装到 config 目录。
- 与 u1-l2 讲过的 `build.sh --tiling_key` 呼应：[CMakeLists.txt:276-280](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L276-L280) 的 `add_ops_tiling_keys(OP_NAME "ALL" TILING_KEYS ${TILING_KEY})`，经 [cmake/func.cmake:212-227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L212-L227) 转成 `--tiling-keys`（或 V2 路径的 `--tiling_key=`）编译选项，实现「只编译指定 TilingKey 的 kernel」的裁剪编译。

注意边界：binary.json 的**运行期签名匹配**逻辑属于 CANN 框架内部，本仓库只负责产出清单；精确的匹配算法不在本仓库源码内（待确认，需查 CANN 文档）。

#### 4.1.4 代码实践

**实践目标**：用纯阅读方式摸清两个 SOC 目录的配置差异与 binary.json 的条目规律。

**操作步骤**（示例命令，在 `inference/ascendc` 目录下执行）：

```bash
# 1. 对比两个 SOC 的 simplified_key.ini 与 binary.json
diff src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_simplified_key.ini \
     src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_simplified_key.ini

# 2. 数一数每个 SOC 各预编译了几份二进制
grep -c bin_filename src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/*/moe_init_routing_v3_binary.json

# 3. 提取所有条目的 x/expanded_x dtype，观察组合规律
grep -A3 '"name": "x"' src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910_93/moe_init_routing_v3_binary.json | grep dtype
```

**需要观察的现象**：diff 无输出（两个 SOC 配置一致）；每个 json 各 7 条 `bin_filename`；x 的 dtype 依次为 int8/fp16/fp32/bf16/fp16/fp32/bf16。

**预期结果**：7 条 = 4 份「原样输出」+ 3 份「量化输出 int8」。若未来某 SOC 需要差异化（如 910_93 多一个变体），只需在该 SOC 子目录的 json 里加条目、必要时在 ini 里加 `ascendxx=xx` 覆盖。

#### 4.1.5 小练习与答案

**练习 1**：binary.json 里 `"shape": [-2]` 是什么意思？为什么不写成具体维度？
**答案**：-2 表示动态形状（任意维度、运行时才确定）。MoE 的 N（token 数）、H（隐藏维）、K（top-k）随 batch 与模型配置变化，若写死维度则每个 shape 都要一份二进制；配合 simplified key 机制，一份二进制可服务一个等价签名集合。

**练习 2**：如果把 `quant_mode` 的默认预编译值从 -1 改成 1（动态量化），binary.json 里的属性表会发生什么连锁要求？
**答案**：属性表 `quant_mode` 的 value 需改为 1，且该变体的 `expanded_x` dtype 必须是 int8（动态量化输出 int8），同时 `scale` 从纯可选变成动态量化语义下的有效输入（shape 规则变为 2 维）。属性取值与输入输出 dtype 表是同一张签名卡的两侧，必须同步修改。

**练习 3**：`bin_filename` 为什么带长哈希后缀？
**答案**：哈希由二进制内容（或其生成参数）决定，起「版本指纹」作用：源码、编译选项、tiling key 集合任一变化，文件名即变化，安装与运行期不会把旧二进制误当成新二进制加载。

### 4.2 模块二：TilingKey 分支——host 编码与 kernel 对号入座

#### 4.2.1 概念说明

MoE V3 的模式组合有 5 个自由度：排序核数（单核/多核/gather-first）、量化（无/静态/动态）、索引类型（GATHER/SCATTER）、DropPad（0/1）、以及一批「快车道」（空张量、全载、性能打点、计数排序）。如果每个组合写一个 tiling 模板类，host 侧会失控。这个算子的做法是：**host 侧只有一个 tiling 模板类，把模式组合编码成一个十进制位段式的 uint64 TilingKey，kernel 侧按 key 逐级分发到模板类**。

「位段式」指 key 的每一段十进制位承载一个模式维度，类似车牌号分段：

\[ \text{Key} = 10^6 + \text{sortMode} \times 10^5 + (\text{quantMode}+1) \times 10^4 + \text{rowIdxType} \times 10^3 + \text{dropPadMode} \times 10^2 \]

其中 sortMode ∈ {0 单核排序, 1 多核排序, 2 gather-first 单核（被计数排序 FullLoad 拦截）, 3 gather-first 多核}，quantMode+1 是为了把 -1（不量化）挪到 0 段位。

#### 4.2.2 核心流程

host 侧选 key 的优先级（从高到低，前面的命中即返回）：

```text
1. 空张量 (n == 0)                    → 3000000
2. IsFullLoad() 为真（UB 装得下全量）   → 非量化 2100000 / 静态 2200000
                                        / 动态 2300000 + ep×10000 + smoothType×1000
3. 性能打点形状 (1,7168)(1,8)(256,7168) → 2000000
4. counting sort FullLoad 适用          → sortMode=2 组合 key（12xxxxx）
5. counting sort 非 FullLoad 适用       → sortMode=3 组合 key（13xxxxx）
6. 通用组合 key                         → 10xxxxx（本讲主线索）
```

kernel 侧则用同一套数值的宏做 `TILING_KEY_IS` 链式分发。**两侧数值由两处独立源码硬编码，一致性靠人肉/UT 维护**——这是本算子最容易踩坑的地方。

#### 4.2.3 源码精读

**（1）host 侧的 key 常量定义。** [ai_infra_moe_init_routing_v3_tiling.cpp:92-105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L92-L105) 定义了位段基数（`TILINGKEY_BASE=1000000`、`SORT_CORE_TILINGKEY_BASE=100000`、`QUANT_MODE_TILINGKEY_BASE=10000`、`ROWIDX_TYPE_TILINGKEY_BASE=1000`、`DROP_MODE_TILINGKEY_BASE=100`）与快车道 key（`EMPTY_TENSOR_TILINGKEY=3000000`、全载系列 2100000/2200000/2300000、性能打点 2000000）。

**（2）GetTilingKey 的完整决策树。** [ai_infra_moe_init_routing_v3_tiling.cpp:1114-1159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1114-L1159) 按上面流程图的优先级逐层返回。通用分支在 [L1152-1158](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1152-L1158)：`sortMode_` 来自 `Tiling4VBSCompute`（[L1266-1281](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1266-L1281)：`totalLength_ <= sortLoopMaxElement` 则单核 `sortMode_=0`，否则多核 `sortMode_=1`）。

**（3）全载判定 IsFullLoad。** [ai_infra_moe_init_routing_v3_tiling.cpp:763-810](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L763-L810) 是一次 UB 预算演算：排序空间 + 行索引空间 + 专家计数空间 + gather 空间 + 量化缓冲，按 quantMode 三选一扣减后，剩余 `remainUb > 0` 才算装得下（[L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L780) 注意：`dropPadMode_ == 1` 直接判不满载——DropPad 场景输出形状是 `[expertNum, capacity, cols]`，映射关系复杂，不走全载快车道）。

**（4）kernel 侧的镜像宏。** [ai_infra_moe_init_routing_v3.cpp:41-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L41-L93) 重新定义了同一套数值，且**每个宏都带中文注释标明场景**，例如 `MOE_INIT_ROUTING_V3_SORTONECORE_GATHER_NODROP 1000000`（单核排序、非量化、GATHER 索引）与 `MOE_INIT_ROUTING_V3_SORTMULTICORE_DYNAMICQUANT_GATHER_DROP 1120100`（多核排序、动态量化、GATHER 索引、DropPad）。手工验算两个：

- 单核 + 非量化(−1→0) + GATHER(0) + 无 drop(0)：\(10^6 + 0 + 0 + 0 + 0 = 1000000\) ✓
- 多核 + 动态量化(1→2) + GATHER(0) + drop(1)：\(10^6 + 10^5 + 2 \times 10^4 + 0 + 10^2 = 1120100\) ✓

**（5）kernel 入口分发。** [ai_infra_moe_init_routing_v3.cpp:97-116](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L97-L116) 是标准 AscendC 入口（参数按 OpDef IO 顺序 + workspace + tiling），[L101-104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L101-L104) 声明 MIX_AIV 任务类型但 `g_coreType == AIC` 直接返回——本算子纯跑向量核。随后从 [L118](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L118) 起是长长的 `TILING_KEY_IS` 链。

**（6）二级运行期分派——key 之内还有谓词。** 值得特别注意的是：同一个 TilingKey 内部，kernel 还会用 TilingData 字段再分派一次。典型例子是计数排序拦截：[ai_infra_moe_init_routing_v3.cpp:256-274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L256-L274) 在 `GATHER_SORTONECORE_*` key 下重算 `useCountingSort` 谓词（`dropPadMode==0 && quantMode==-1 && actualExpertNum<=128 && …`），为真则改走计数排序模板。而 host 侧 [ai_infra_moe_init_routing_v3_tiling.cpp:812-823](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L812-L823) 的 `ComputeCountingSortTiling` 用的是**同一谓词**决定是否把 sortMode 编成 2/3。两侧谓词一旦有一边改了另一边没改，行为就会分裂——这是维护此类算子最重要的纪律。

**（7）UT 是 key 一致性的安全网。** [test_ai_infra_moe_init_routing_v3_tiling.cpp:478-489](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/ut/op_host/test_ai_infra_moe_init_routing_v3_tiling.cpp#L478-L489) 的用例 `moe_init_routing_v3_tiling_01` 直接把期望 TilingKey `1000000` 写进断言（N=1859、非量化、DropLess、GATHER、count 模式），[L491-502](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/ut/op_host/test_ai_infra_moe_init_routing_v3_tiling.cpp#L491-L502) 的用例 02 断言 scatter 版本为 `1001000`——纯 CPU 就能验证 host 编码。

**（8）tiling 的双文件挂接。** host 侧分两个文件完成注册：[ai_infra_moe_init_routing_v3_tiling_base.cpp:51-53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling_base.cpp#L51-L53) 用 `IMPL_OP_OPTILING` 把 `TilingForMoeInitRoutingV3` 挂成框架 tiling 入口、`TilingParse` 挂编译期平台解析；[ai_infra_moe_init_routing_v3_tiling.cpp:1583-1584](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1583-L1584) 再把模板类 `MoeInitRountingV3TilingBase` 以 priority=10000 注册进注册表（注释「If not 950, fallback to this」提示 950 平台另有模板，但本仓库未提供）。运行期 [ai_infra_moe_init_routing_v3_tiling_base.cpp:21-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling_base.cpp#L21-L24) 调 `TilingRegistry::GetInstance().DoTilingImpl(context)`，注册表按 priority 升序轮询、`GRAPH_PARAM_INVALID` 则试下一个（机制详见 u5-l1）。平台信息不走运行期查询而是读编译期缓存：[ai_infra_moe_init_routing_v3_tiling.cpp:324-334](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L324-L334) 从 `GetCompileInfo()` 取 `MoeInitRoutingV3CompileInfo`（该结构在 [ai_infra_moe_init_routing_v3_tiling.h:143-147](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.h#L143-L147) 定义，缓存 aivNum/ubSize/socVersion 三个字段，由 [ai_infra_moe_init_routing_v3_tiling_base.cpp:26-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling_base.cpp#L26-L49) 的 TilingParse 阶段填充）。

#### 4.2.4 代码实践

**实践目标**：不依赖硬件，手工演算 TilingKey 并与 kernel 宏、UT 断言三方核对。

**操作步骤**：

1. 读 [ai_infra_moe_init_routing_v3_tiling.cpp:1114-1159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1114-L1159)，对下表每行代入编码公式算出 key：

| 场景 | sortMode | quantMode | rowIdxType | dropPadMode | 你的答案 |
|---|---|---|---|---|---|
| 单核、非量化、GATHER、无 drop | 0 | -1 | 0 | 0 | ？ |
| 单核、非量化、SCATTER、无 drop | 0 | -1 | 1 | 0 | ？ |
| 多核、静态量化、GATHER、无 drop | 1 | 0 | 0 | 0 | ？ |
| 多核、动态量化、SCATTER、无 drop | 1 | 1 | 1 | 0 | ？ |
| 多核、非量化、GATHER、drop | 1 | -1 | 0 | 1 | ？ |
| 多核、动态量化、GATHER、drop | 1 | 1 | 0 | 1 | ？ |

2. 把答案与 [ai_infra_moe_init_routing_v3.cpp:54-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L54-L93) 的宏逐一比对。
3. 有昇腾环境时运行 UT 复核（无环境则标注待本地验证）：`bash build.sh -u --ophost` 触发 op_host UT（构建入口详见 u6-l1），观察 gtest 输出中 `moe_init_routing_v3_tiling_01/02` 是否通过。

**需要观察的现象**：演算值与 kernel 宏数值一字不差；UT 断言的 `expectTilingKey` 与你的演算一致。

**预期结果**：六行答案依次为 1000000、1001000、1110000、1121000、1100100、1120100。若任何一行对不上，说明你把某个位段的进位规则理解错了（常见错误：忘记 quantMode 要 +1）。

#### 4.2.5 小练习与答案

**练习 1**：为什么编码公式里是 `(quantMode + 1)` 而不是直接 `quantMode`？
**答案**：quantMode 取值 -1/0/1，直接乘位段会出现负数贡献（如 -1×10000），破坏 key 的单调可读性。+1 把三档映射到 0/1/2，保证每位段非负，也让人可以从 key 反推出模式。

**练习 2**：动态量化全载分支的 key 为什么是 `2300000 + ep×10000 + smoothType×1000`，而不是复用通用公式？
**答案**：全载是一条独立的快车道（`IsFullLoad` 为真时不走排序流水线，一个模板类一次做完），它的两个剩余自由度是 ep（是否专家子集模式，来自 expert_range 是否全长）与 smoothType（动态量化 scale 是 1×H 还是 E×H，由 scale 输入的第 0 维推断，见 [ai_infra_moe_init_routing_v3_tiling.cpp:542-568](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L542-L568)），分别占用 10^4 与 10^3 位段——与 kernel 宏 `DYNAMIC_QUANT_GATHER_NO_SCALE_FULLLOAD 2300000`、`DYNAMIC_QUANT_SCATTER_EH_SCALE_FULLLOAD 2312000` 等一一对应。

**练习 3**：host 侧把 sortMode 从 1 改成 2（假设新增一种排序策略），最少要同步改几处？
**答案**：至少三处——host 的 `GetTilingKey`/sortMode 计算、kernel 入口对应数值的宏定义、以及 UT 中受影响用例的 `expectTilingKey`。若新 sortMode 还伴随 counting-sort 式的二级谓词，则 host 与 kernel 的谓词表达式也要同步。漏改 kernel 宏会导致该场景静默落错分支。

### 4.3 模块三：kernel 模板组合——30 余个 moe_v3_* 头文件的模块化设计

#### 4.3.1 概念说明

kernel 入口的 `TILING_KEY_IS` 链不是「一个 key 对应一个类」这么简单，而是**流水线分段**：一次 MoE 路由被拆成「排序 → 直方图统计 → 位置映射（srcToDst）→ 搬运输出（gather）」四个子阶段，每个子阶段独立按 TilingKey + TilingData 字段选择自己的 kernel 类。于是文件按职能分成六族：

| 族 | 代表文件 | 职责 |
|---|---|---|
| 公共 | `moe_v3_common.h` | 常量（GATHER/SCATTER、SCALE 模式）、assist 查表、对齐工具 |
| 排序 | `moe_v3_sort_base.h`、`sort_one_core`、`sort_multi_core`、`mrgsort*`、`gather_sort_multi_core`、`sort_actual_expert` | expert_idx 排序 |
| 计数排序 | `moe_v3_cut_origin_t.h`、`moe_v3_full_load_cut_origin_t.h` | 小专家数场景按专家计数代替比较排序 |
| 全载 | `moe_v3_full_load*.h`（5 个） | UB 一次装下全量，单类完成全流程 |
| 统计 | `moe_v3_expert_tokens_count.h` | count/cumsum/key_value 三模式直方图 |
| 搬运 | `row_idx_gather*`、`gather_out*`、`gather_*_quant`、`gather_droppad_*` | 按映射搬 x 并做量化/droppad |

模式组合则靠 **C++ 模板参数**正交展开，例如 `MoeV3FullLoadDynamicQuant<DTYPE_X, GATHER|SCATTER, NO_SCALE|SCALE_1H|SCALE_EH>` 用两个整型模板参数一次实例化 6 个变体（[moe_v3_common.h:47-52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_common.h#L47-L52) 定义了这些常量）。这比「每组合一个文件」节省大量重复代码。

#### 4.3.2 核心流程

kernel 入口的完整分发结构（自上而下顺序执行，命中哪段跑哪段）：

```text
TILING_KEY_IS(3000000)  空张量 → 只清零 expert_tokens 输出，return
TILING_KEY_IS(2000000)  性能打点 → MoeV3FullLoad，return
TILING_KEY_IS(23xxxxx)  动态/静态/非量化全载 → MoeV3FullLoad* 系列，return
TILING_KEY_IS(12xxxxx / 13xxxxx)
    ├─ 满足计数排序谓词 → MoeV3FullLoadCutOriginT / MoeV3CutOriginT，return
    └─ 否则 fall through 原始 gather-first 流程:
         MoeSortActualExpert（单核）→ MoeGatherSortMultiCore（多核）
         → MoeSortMultiCorePerformance
10xxxxx 通用流水线:
    ① 排序:  MoeSortOneCore（单核 key）| MoeSortMultiCore（多核 key）
    ② 统计:  ExpertTokensCount<CUMSUM|COUNT|KEY_VALUE>
              （gather-first key 或 dropPad/ep/flag 场景才执行，运行期按 t->expertTokensNumType 选）
    ③ 映射:  drop 场景 MoeV3SrcToDstWithCapacity（非量化/静态量化，后者强制 int8）
              | 动态量化 drop 场景 MoeV3SrcToDstAndGather（映射+搬运融合，return 前已完成）
              | 非 drop 场景 RowIdxGather
    ④ 搬运:  非 drop MoeGatherOut / MoeGatherOutDynamicQuant / MoeGatherOutQuant
              | 非量化 drop MoeGatherOutDroppad | 静态量化 drop MoeGatherDroppadQuant
              （动态量化 drop 已在③融合，不再进入）
```

另一个编译期剪枝技巧贯穿始终：量化类模板都包在 `if constexpr (!IsSameType<DTYPE_X, int8_t>::value)` 里（如 [ai_infra_moe_init_routing_v3.cpp:427](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L427)），因为 int8 输入不允许再量化（host 侧 [ai_infra_moe_init_routing_v3_tiling.cpp:996-998](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L996-L998) 有对应检查），编译期直接剔除实例。

#### 4.3.3 源码精读

**（1）排序族基类。** [moe_v3_sort_base.h:23-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_sort_base.h#L23-L63) 的 `MoeSortBase` 定义了排序族共享的设施：`sortDataCopyInQueue`/`sortDataCopyOutQueue` 双向队列、`tempBuffer`/`sortedBuffer` 计算缓冲、6 个 `GlobalTensor`（专家索引、展开行号、排序中间量等）以及 `expertStart_/expertEnd_/ep_` 等从 TilingData 恢复的模式字段。单核/多核/mrgsort 各类继承它，只实现各自的 `Init/Process`。这体现了 kernel 侧的类设计套路：**基类管共享状态，子类管切分策略**。

**（2）TilingData 主结构的「子阶段分包」。** [ai_infra_moe_init_routing_v3_tiling.h:111-141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.h#L111-L141) 的 `MoeInitRoutingV3TilingData` 先放 21 个全局标量（coreNum/n/k/cols/quantMode/rowIdxType/dropPadMode/ep/smoothType…），再用 `TILING_DATA_FIELD_DEF_STRUCT` 内嵌 8 个子结构，每个子结构对应 kernel 的一个子阶段，例如 [L28-39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.h#L28-L39) 的 `MoeV3VBSComputeTilingData`（排序阶段的 needCoreNum/perCoreLoops 等 10 个字段）与 [L82-96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.h#L82-L96) 的 `MoeV3SrcToDstCapacityComputeTilingData`（DropPad 映射阶段的行列切分）。注意 [L138-139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.h#L138-L139) 用**同一个子结构类型**声明了两个字段（`srcToDstDropPadParamsOp` 与 `srcToDstDropPadDynamicParamsOp`）——非动态量化与动态量化两条 DropPad 切分各自一套参数，kernel 按场景取其一。host 侧的分流在 [ai_infra_moe_init_routing_v3_tiling.cpp:1349-1354](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1349-L1354)：动态量化 + DropPad 走 `Tiling4SrcToDstDropPadDynamicCompute`（[L1441-1475](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1441-L1475)，UB 预算按 15 份列缓冲 + 64B scale 估算）。

**（3）DoOpTiling 的子阶段总装。** [ai_infra_moe_init_routing_v3_tiling.cpp:1060-1107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1060-L1107) 依次调 `CheckAttr → CheckInputShape → CheckOutShape → CheckDtype` 四层检查，再按序调 7 个 `Tiling4*` 填充对应子结构（[L1094-1100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1094-L1100)：VBS 排序、VMS 中间归并、SortOut、ExpertTokensCount、SrcToDst、SrcToDstDropPad、GatherOut），最后 [L1101-1104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1101-L1104) 判全载与计数排序。workspace 也是分项求和（[L1161-1191](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1161-L1191)：排序 + 核间同步 + scatter + 专家计数等，动态量化追加量化临时区、DropPad 追加专家索引值区，计数排序若需求更大则整体抬升）。

**（4）性能打点模板 MoeV3FullLoad 精读。** [moe_v3_full_load.h:21-78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L21-L78) 是为「BF16、N=1（decode 单 token）、256 专家、动态量化、key_value 统计」这一特定高频场景定制的（host 侧 [ai_infra_moe_init_routing_v3_tiling.cpp:937-965](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L937-L965) 精确匹配该形状才启用，且 [L1087-1089](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1087-L1089) 把 aivNum 直接改成 totalLength_——**每个核只处理一个 token**）。它示范了三个 kernel 编写技巧：

- [SortCompute，L92-130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L92-L130)：用 AscendC 内置排序指令 `Sort<float, true>`（降序）+ `Extract` 抽取 (key, 原位置) 对，排序前把 expert_idx 取负转 fp32、尾部对齐段填充 `MIN_FP32` 占位——用向量指令完成 key-value 排序。
- [ExpertCountCompute，L132-164](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L132-L164)：排序后专家 id 天然分段连续，标量 `GetValue` 顺序扫描遇变值即累计一段，直接产出 key_value 直方图——「先排序后统计」把直方图降为一次线性扫描。
- [CopyOutDynamicQuant，L166-229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L166-L229)：每核对一个 token 做 per-token 动态量化：`x × smooth → Abs → ReduceMax` 得幅值最大值，`scale = amax / 127`，再 `Div(scale) → Cast(fp16) → Cast(int8)` 双步取舍写回 `expanded_x`，scale 写回 `expanded_scale`（[L193-201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L193-L201)）。GATHER/SCATTER 索引的输出差异在 [L213-227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/moe_v3_full_load.h#L213-L227)：SCATTER 直接顺序写出，GATHER 需按 `dstIdx` 散写（`SetValue(dstIdx, i)`）后再拷出。

**（5）通用流水线的分发源码。** 排序段 [ai_infra_moe_init_routing_v3.cpp:333-362](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L333-L362) 把 9 个单核 key 归到 `MoeSortOneCore`、9 个多核 key 归到 `MoeSortMultiCore`；映射段 [L408-441](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L408-L441) 区分非量化 DropPad（`MoeV3SrcToDstWithCapacity<DTYPE_X,…>`）、静态量化 DropPad（强制 `<int8_t,…>`）、动态量化 DropPad（`MoeV3SrcToDstAndGather`，融合搬运）、非 Drop（`RowIdxGather`）；搬运段 [L443-523](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L443-L523) 再按量化/droppad/ep 选 `MoeGatherOut` 系列，其中动态量化非 Drop 场景在 [L471-481](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L471-L481) 还按 `t->ep`/`t->smoothType` 运行期再选 GATHER/SCATTER 实例。

#### 4.3.4 代码实践

**实践目标**：给 30 个 kernel 头文件建一张「族谱」，并跟踪一个具体 TilingKey 的完整执行序列。

**操作步骤**：

```bash
# 1. 列出全部 kernel 头文件并按文件名前缀归族
ls src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/*.h | xargs -n1 basename

# 2. 数一下每个族的数量（示例命令）
ls src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ | grep -c "sort"
ls src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ | grep -c "gather"
ls src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ | grep -c "full_load"
```

3. 打开 [ai_infra_moe_init_routing_v3.cpp:333-523](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L333-L523)，为 TilingKey=1020100（单核、动态量化、GATHER、DropPad）写出按顺序执行的 kernel 类清单及其所在头文件。
4. 进每个类的 `Init` 看 `pipe->InitBuffer` 划拨了哪些队列/缓冲，对照 host 侧对应子结构的字段（如 `srcToDstDropPadDynamicParamsOp`）。

**需要观察的现象**：文件恰好 30 个（不含入口 cpp）；sort/gather/full_load 三个前缀覆盖了绝大多数文件；1020100 场景在映射段之后就结束（不再进搬运段）。

**预期结果**：1020100 的执行序列为 `MoeSortOneCore`（moe_v3_sort_one_core.h）→ `ExpertTokensCount<COUNT_MODE>`（moe_v3_expert_tokens_count.h，DropPad 场景必执行统计）→ `MoeV3SrcToDstAndGather`（moe_v3_row_idx_gather_droppad_dynamic.h，映射+动态量化搬运一步完成）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MoeV3SrcToDstAndGather` 要把映射和搬运融合成一个类，而静态量化 DropPad 却分成 `MoeV3SrcToDstWithCapacity` + `MoeGatherDroppadQuant` 两步？
**答案**：动态量化需要对每个 token 现场算 scale（依赖该 token 的数据幅值），映射产生的目的位置与量化计算天然耦合，融合可避免中间结果落 GM 再读回；静态量化的 scale/offset 是现成输入，映射与搬运解耦也不会引入额外读写，拆开能让两段各自复用非量化的切分参数（host 侧同一份 `srcToDstDropPadParamsOp` 即可服务非量化与静态量化两分支）。

**练习 2**：`moe_v3_common.h` 里的 `assist[256]` 查表是干什么的？
**答案**：它是一个 256 元素的 int32 表，每 8 个一组，组内第 0 个位置放组号（0,1,2,…31）、其余为 0。kernel 里利用向量指令按 lane 取值时，用它把「元素在 repeat 内的位置」批量映射成编号（如分组归约的下标），等价于设备侧的小型常量数据库——避免在循环里逐元素算编号。

**练习 3**：如果新增一种「fp8 量化」模式（quant_mode=2），按现有架构要动哪些文件？
**答案**：host 侧——`CheckAttr` 的 quantMode 合法值集合、`IsFullLoad` 的 UB 预算分支、`Tiling4GatherOut*` 的 colMultiple/rowMultiple 系数、可能新增 TilingKey 位段取值；kernel 侧——入口新增对应 TilingKey 宏与分发分支、新增 `moe_v3_gather_fp8_quant.h` 等模板（或在动态量化模板加 `SCALE_FP8` 模板参数）、`if constexpr` 排除 int8 输入；配置侧——binary.json 若要预编译 fp8 变体需加条目；测试侧——UT 增期望 key 用例、ST 增对拍用例。这正是「模式组合爆炸」的代价，也是该算子用模板参数而非文件数硬扛组合数的原因。

## 5. 综合实践

**任务**：制作『场景 → TilingKey → kernel 宏 → kernel 类/头文件』对照表，并论证 droppad + 动态量化组合的正确选型。

**第一部分：对照表（参考答案）**。

| 场景 | TilingKey | kernel 宏（入口 cpp） | kernel 类（头文件） |
|---|---|---|---|
| 空张量 | 3000000 | `EMPTY_TENSOR` | 入口内联清零（无独立类） |
| 性能打点 decode | 2000000 | `MOE_INIT_ROUTING_V3_PERFORMANCE` | `MoeV3FullLoad`（moe_v3_full_load.h） |
| 全载·非量化 | 2100000 | `UNQUANTIZED_FULLLOAD` | `MoeV3FullLoadUnquantized<DTYPE_X>`（full_load_unquantized.h） |
| 全载·静态量化 | 2200000 | `STATIC_QUANT_FULLLOAD` | `MoeV3FullLoadStaticQuant<DTYPE_X>`（full_load_static_quant.h） |
| 全载·动态量化 6 变体 | 2300000~2312000 | `DYNAMIC_QUANT_*_FULLLOAD` | `MoeV3FullLoadDynamicQuant<DTYPE_X, G|S, scale 模式>`（full_load_dynamic_quant.h） |
| 计数排序 FullLoad | 1200000/1201000 | `GATHER_SORTONECORE_*`（拦截） | `MoeV3FullLoadCutOriginT<DTYPE_X>`（full_load_cut_origin_t.h） |
| 计数排序非 FullLoad | 1300000/1301000 | `GATHER_SORTMULTICORE_*`（拦截） | `MoeV3CutOriginT<DTYPE_X>`（cut_origin_t.h） |
| gather-first 原流程 | 12xxxxx/13xxxxx（谓词不满足） | 同上（不拦截） | `MoeSortActualExpert` + `MoeGatherSortMultiCore` + `MoeSortMultiCorePerformance` |
| 单核·非量化·GATHER | 1000000 | `SORTONECORE_GATHER_NODROP` | `MoeSortOneCore` → `RowIdxGather` → `MoeGatherOut<DTYPE_X,0>` |
| 单核·非量化·SCATTER | 1001000 | `SORTONECORE_SCATTER_NODROP` | 同上（gather 段按 ep 选模板） |
| 多核·非量化·GATHER | 1100000 | `SORTMULTICORE_GATHER_NODROP` | `MoeSortMultiCore`（+ mrgsort 归并族）→ 同上后半 |
| 单/多核·静态量化 | 1010000/1110000 等 | `*QUANT_*_NODROP` | + `MoeGatherOutQuant<DTYPE_X, ep>`（gather_static_quant.h） |
| 单/多核·动态量化 | 1020000/1120000 等 | `*DYNAMICQUANT_*_NODROP` | + `MoeGatherOutDynamicQuant<DTYPE_X, G|S>`（gather_dynamic_quant.h） |
| 单/多核·非量化·DropPad | 1000100/1100100 | `*_GATHER_DROP` | + `MoeV3SrcToDstWithCapacity`（row_idx_gather_droppad.h）+ `MoeGatherOutDroppad`（gather_out_droppad.h） |
| 单/多核·静态量化·DropPad | 1010100/1110100 | `*QUANT_GATHER_DROP` | + `MoeV3SrcToDstWithCapacity<int8_t,…>` + `MoeGatherDroppadQuant`（gather_droppad_static_quant.h） |
| 单/多核·动态量化·DropPad | 1020100/1120100 | `*DYNAMICQUANT_GATHER_DROP` | `MoeV3SrcToDstAndGather`（row_idx_gather_droppad_dynamic.h，融合完成） |

**第二部分：droppad + 动态量化选型论证**。

给定输入：`drop_pad_mode=1`、`quant_mode=1`（动态量化）、`row_idx_type=0`、`expert_range=[0, expertNum]`。逐层推导：

1. **全载排除**：[IsFullLoad 的 L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L780) 对 `dropPadMode_ == 1` 直接返回 false → 不走 23xxxxx 快车道。
2. **性能打点排除**：该分支要求特定 decode 形状且与 DropPad 互斥（打点条件含 `expertTokensNumType_ == KEY_VALUE` 的特定组合，见 [L937-965](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L937-L965)）。
3. **计数排序排除**：[ComputeCountingSortTiling 的门卫 L815](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L815) 要求 `quantMode_ == UN_QUANT`，动态量化不满足。
4. **属性约束**：[CheckAttr 的 L385-397](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L385-L397)：DropPad 模式强制 `row_idx_type` 必须为 GATHER(0)、expert_range 必须全长（ep=0）、expert_capacity ∈ (0, n]；若 `expert_tokens_num_flag=true` 则 type 只能是 COUNT。
5. **落位通用公式**：key = \(10^6 + \text{sortMode} \times 10^5 + 2 \times 10^4 + 0 + 10^2\)，sortMode 由 `totalLength = n×k` 与 `sortLoopMaxElement`（由 UB 大小推得）比较而定——小批量得 **1020100**（`MOE_INIT_ROUTING_V3_SORTONECORE_DYNAMICQUANT_GATHER_DROP`），大批量得 **1120100**（多核版）。
6. **kernel 实例**：分发落在 [ai_infra_moe_init_routing_v3.cpp:424-434](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_kernel/ai_infra_moe_init_routing_v3.cpp#L424-L434) → `MoeV3SrcToDstAndGather<DTYPE_X, MoeInitRoutingV3TilingData>`（moe_v3_row_idx_gather_droppad_dynamic.h），且 `DTYPE_X` 不能是 int8（`if constexpr` 剪枝）；其切分参数来自 host 的 `Tiling4SrcToDstDropPadDynamicCompute` 填充的 `srcToDstDropPadDynamicParamsOp` 字段；workspace 额外叠加量化临时区与 DropPad 专家索引区（[GetWorkspaceSize 的 L1179-1184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_tiling.cpp#L1179-L1184)）。

**验证方式**：在 UT 里加一个 DropPad+动态量化用例（仿照 `RunNormalCaseNoQuantDroppad` 的输出 shape `[expertNum, C, H]` 构造，[test_ai_infra_moe_init_routing_v3_tiling.cpp:169-205](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/ut/op_host/test_ai_infra_moe_init_routing_v3_tiling.cpp#L169-L205)），断言 `expectTilingKey=1020100`——纯 CPU 可验（运行命令待本地验证，UT 框架细节见 u6-l1）。

## 6. 本讲小结

- **SOC 配置**：`op_host/config/<soc>/` 下 `binary.json` 是预编译二进制清单（本算子 7 条，覆盖 4 原样 + 3 量化 int8 的 dtype 组合，`bin_filename` 带内容哈希），`simplified_key.ini` 控制 opc 的 `--simplified_key_mode`（本算子 `default=0`）；910b 与 910_93 两套配置当前完全一致。
- **TilingKey 编码**：通用 key 按 \(10^6 + \text{sortMode}\times10^5 + (\text{quantMode}+1)\times10^4 + \text{rowIdxType}\times10^3 + \text{dropPadMode}\times10^2\) 位段合成，另有空张量(3000000)、全载(21/22/23xxxxx)、性能打点(2000000)三条快车道；host 与 kernel 两处独立硬编码，靠 UT 断言 `expectTilingKey` 兜底。
- **host 单模板 + kernel 多模板**：本算子 host 侧只注册一个 tiling 模板类（priority=10000），模式组合的展开下沉到 kernel 侧——30 个 `moe_v3_*` 头文件按排序/计数排序/全载/统计/搬运分族，靠 C++ 模板参数（DTYPE × GATHER/SCATTER × scale 模式 × ep）正交实例化。
- **流水线分段分发**：kernel 入口按「排序 → 直方图 → srcToDst 映射 → gather 搬运」四段各自按 TilingKey + TilingData 字段选类；动态量化 DropPad 场景用 `MoeV3SrcToDstAndGather` 把映射与量化搬运融合为一步。
- **镜像谓词纪律**：计数排序等二级判定在 host（决定 sortMode 编码）与 kernel（`useCountingSort` 重算）各写一遍，两侧必须同步修改，否则行为分裂。
- **编译期剪枝**：量化路径统一包在 `if constexpr (!IsSameType<DTYPE_X, int8_t>)` 中，int8 输入不允许量化，直接从实例层面剔除非法组合。

## 7. 下一步学习建议

- **u4-l6（MHC 算子）**：看另一个方向的模块化——少量文件内的 single_tile/multi_tile、单核/双核分支，与本章的「多文件模板族」对照。
- **u4-l7（kv_rms_norm_rope_cache）**：观察 kernel 变体的**命名规律**（b16/pa/nz/mtp/quant/arch35 后缀），思考它与本章「TilingKey 位段编码」在变体管理思路上的差异。
- **u5-l1（公共 Tiling 框架深入）**：本章只用了 `REGISTER_OPS_TILING_TEMPLATE` 的单模板注册；u5-l1 系统讲解 priority 轮询、TilingKey 编码约定与 tiling 缓存，把本章的注册表机制补全。
- **u6-l1（UT 单测框架）**：本章反复依赖 UT 断言 `expectTilingKey` 做 key 一致性安全网，建议紧接着学 tiling_context_faker 的实现，掌握为自己的算子写 tiling UT 的能力。
- 源码延伸阅读：`op_kernel/moe_v3_gather_dynamic_quant.h` 与 `moe_v3_row_idx_gather_droppad_dynamic.h`（动态量化两条路径的完整实现），以及 `op_api/aclnn_ai_infra_moe_init_routing_v3.cpp`（本讲未展开的接口层，套路与 u2-l2 相同）。
