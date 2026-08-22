# u2-l1 op_host 之 OpDef：算子原型注册

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂仓库中任何一个 `*_def.cpp` 文件：知道 `Input`/`Output`/`Attr` 三类声明各在描述什么。
2. 看懂 `ParamType`（REQUIRED / OPTIONAL / DYNAMIC）与 `DataType`、`Format`、`UnknownShapeFormat` 三个列表的「按下标对齐成组合」语义，能数出一个算子支持多少种类型组合。
3. 理解 `OpAICoreConfig` 上各个能力开关（动态 shape、动态格式、精度下降等）和 `ExtendCfgInfo` 扩展配置的工程含义。
4. 掌握 `AddConfig("ascend910b" / "ascend910_93", ...)` 为不同昇腾芯片版本登记同一算子的方式。
5. 说清「算子被发现」的两层机制：编译期靠目录 + CMakeLists 被 GLOB 收集，运行期靠 `OP_ADD` 宏把 OpDef 登记进算子原型库 `cust_opsproto_rt2.0.so`。
6. 亲手为一个假想的 `my_add` 算子写出一份合规的 `my_add_def.cpp`。

## 2. 前置知识

本讲建立在 u1-l3（算子目录解剖）之上，先把几个概念用通俗语言补齐：

- **算子原型（op proto）**：可以类比 C 语言里的「函数声明」。`int add(int x, int y);` 只声明参数和返回值，不写实现。OpDe­f 声明的就是算子的「签名」：有几个输入输出、每个张量支持什么数据类型和格式、有哪些标量属性、跑在哪类芯片上。框架（图引擎 GE、aclnn 执行器）在下发计算前，都要先查这张「签名表」来校验参数、匹配实现。
- **注册表模式（registry）**：C++ 里常见的技巧——用一个全局静态对象，在动态库被加载（`dlopen`）时自动执行构造函数，把「自己」登记进一张全局表。CANN 的 `OP_ADD` 宏就是这个套路：它来自 CANN 开发套件的头文件 `register/op_def_registry.h`（本仓库中没有这个文件，它在容器内的 CANN 安装目录里；宏的内部展开不属于仓库代码，**待确认**，但其效果是静态注册，仓内所有 def 文件都以它收尾）。
- **Host 侧与 Device 侧**（承接 u1-l3）：Host（CPU）负责「算计划」，Device（NPU）负责「执行」。def 文件属于 op_host 层，它完全不包含计算逻辑，只描述「算子长什么样」。
- **op_host 层内三兄弟的分工**：`*_def.cpp`（本讲，登记原型）→ `*_tiling.cpp/.h`（下一讲，算切分参数）→（二者共同编译进 host 侧的库）。aclnn 接口在 op_api 层，不属于本讲。
- **静态 shape 与动态 shape**：静态 shape 指每个维度在编译期就确定；动态 shape 指某些维度是 `-1`，要到运行时才知道实际长度。推理框架里 batch、序列长度经常变化，所以本仓库几乎所有算子都开启动态 shape 支持。
- **SOC 版本**：即具体的昇腾芯片型号系列。本仓库的 def 文件里反复出现 `ascend910b`（910B 系列）与 `ascend910_93`（`build.sh -c` 的默认目标版本，见 u1-l2）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L1-L70) | 最简标本：无 Attr，全 REQUIRED 输入，适合入门精读 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L1-L116) | 进阶标本：展示 OPTIONAL 输入与 Attr（标量属性）声明、`ExtendCfgInfo` |
| [ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L1-L225) | 大型标本：DYNAMIC 输入、`DataTypeList`/`FormatList`/`AutoContiguous`/`ValueDepend`、带默认值的 Attr、`OP_ADD` 双参数形式 |
| [ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_compile_info.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_compile_info.h#L15-L34) | `OP_ADD` 第二参数所绑定的「编译信息」结构体定义处 |
| [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L1-L64) | 算子自己的构建脚本：把 def.cpp 挂进 `op_host_aclnnInner` 目标 |
| [ascendc/cmake/func.cmake](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L41-L80) | `op_add_subdirectory` 函数：GLOB 发现算子目录并按 `-n` 过滤 |
| [ascendc/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L91-L97) | 顶层构建：`op_host_aclnnInner`/`opsproto` 等目标的创建与产出名 |

## 4. 核心概念与源码讲解

### 4.1 OpDef：给算子办一张「身份证」

#### 4.1.1 概念说明

一个自定义算子要能被昇腾软件栈认识，第一步不是写计算代码，而是**登记身份**：

- 它叫什么名字（类名，如 `AiInfraScatterBlockUpdate`，即算子类型名，去掉 `AiInfra` 前缀后就是业务名）。
- 它有哪些输入张量、输出张量，各自支持什么数据类型与内存格式。
- 它有哪些标量属性（Attr），比如激活模式、块大小。
- 它跑在哪种芯片（SOC）上，支持哪些动态能力。

这些信息全部写在一个继承自 `OpDef` 的类里，放在 `op_host/*_def.cpp` 中。`OpDef` 基类与 `OP_ADD` 宏都由 CANN 头文件 `register/op_def_registry.h` 提供（不在本仓库，见前置知识）。

为什么需要它？因为框架侧的三个消费者都要查这张表：

1. **aclnn 执行器**（op_api 层）：组装 `aclOpExecutor` 时按原型核对输入个数与 dtype。
2. **图引擎（GE）**：构图时按原型做 dtype/format 推导与校验。
3. **编译系统**：按原型为不同 SOC 生成/匹配二进制。

#### 4.1.2 核心流程

一个 def 文件从源码到「生效」的完整路径：

```text
写 def.cpp
   │  算子自己的 CMakeLists.txt
   ▼
target_sources(op_host_aclnnInner PRIVATE ..._def.cpp)     ← 编译期挂接
   │  顶层 CMakeLists 读取该目标源码列表
   ▼
为每个算子生成 *_proto.cpp / *_proto.h                      ← 代码生成（autogen）
   │
   ▼
编入 opsproto 目标 → 产出 cust_opsproto_rt2.0.so            ← 算子原型库
   │  run 包安装到 vendors 目录后
   ▼
so 被 dlopen 加载 → OP_ADD 注册的静态对象构造 → 算子进注册表  ← 运行期发现
   │
   ▼
框架按算子名查表，拿到 Input/Output/Attr/SOC 配置
```

注意「被发现」有两层：**编译期**靠目录与 CMakeLists（仓库可控），**运行期**靠 `OP_ADD` 静态注册（CANN 机制）。两层缺一不可——第 4.4 节会精读编译期这一层。

#### 4.1.3 源码精读

先看最简标本 ScatterBlockUpdate 的整体骨架：

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:15-21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L15-L21) —— 引入 CANN 的注册头文件，在 `namespace ops` 中定义类 `AiInfraScatterBlockUpdate` 公有继承 `OpDef`。类名就是算子类型名；构造函数把名字透传给基类：`explicit AiInfraScatterBlockUpdate(const char* name) : OpDef(name)`。

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:22-54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L22-L54) —— 构造函数体就是「填表」：依次声明 `input`、`indices`、`update` 三个输入和一个输出（输出也叫 `input`，因为这是原地更新算子——见 u1-l3 讲过的 `CreateView` 原地写回）。每个声明都是一条链式调用：`ParamType(...)` → `DataType({...})` → `Format({...})` → `UnknownShapeFormat({...})`。

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:56-67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L56-L67) —— 定义一个局部 `OpAICoreConfig aicConfig`，打开四个动态能力开关，然后 `this->AICore().AddConfig("ascend910b", aicConfig)` 与 `AddConfig("ascend910_93", aicConfig)` 把同一份配置登记到两种 SOC。最后 `OP_ADD(AiInfraScatterBlockUpdate);` 收尾完成注册。

把骨架抽出来，仓库里所有 def 文件都是同一个模子：

```text
#include "register/op_def_registry.h"
namespace ops {
class <大驼峰类名> : public OpDef {
public:
    explicit <类名>(const char* name) : OpDef(name) {
        this->Input("名字").ParamType(...).DataType({...}).Format({...}).UnknownShapeFormat({...});
        ...（更多 Input / Output / Attr）
        OpAICoreConfig cfg;
        cfg.<能力开关>(...)...;
        this->AICore().AddConfig("<soc>", cfg);
    }
};
OP_ADD(<类名>);
}
```

命名约定（从仓内文件归纳）：目录与文件名用小写下划线（`ai_infra_scatter_block_update_def.cpp`），类名用大驼峰（`AiInfraScatterBlockUpdate`）；构建脚本按「去掉 `_def` 后缀」反推算子名（见 4.4.3）。

#### 4.1.4 代码实践

**实践目标**：验证「所有 def 文件共享同一骨架」，并统计仓库里 def 文件的数量。

**操作步骤**：

1. 在 `ascendc/src` 下执行 `Glob` 或 `find`，列出所有 `*_def.cpp`。
2. 随意挑 3 个（建议：scatter、conv1d、gqa 之外再挑一个，如 `lower_triangular_inverse_def.cpp`），对照 4.1.3 的模子逐项勾选：Input 声明、Output 声明、Attr 声明、OpAICoreConfig、AddConfig 的 SOC 列表、OP_ADD 行。
3. 记录每个文件的：输入个数、输出个数、Attr 个数、SOC 个数、`OP_ADD` 是否带第二参数。

**需要观察的现象**：所有文件都能套进同一个模子；差异只出现在「条目数量」与「个别可选写法」（如 `FormatList`、`AutoContiguous`、`ExtendCfgInfo`）。

**预期结果**：得到一张对比表，例如 scatter 是 3 输入 / 1 输出 / 0 Attr / 2 SOC / 单参数 `OP_ADD`；conv1d 是 11 输入（含 8 个 OPTIONAL）/ 2 输出 / 9 Attr / 2 SOC / 单参数 `OP_ADD`；gqa 是 27 输入 / 2 输出 / 18 Attr / 2 SOC / 双参数 `OP_ADD`。复杂度递增正好对应三个标本的讲解顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 def 文件里没有任何计算代码，算子却能「跑起来」？

**参考答案**：def 只负责登记原型（签名），供框架查表校验；真正执行依赖另外两层——op_host 的 tiling（算切分方案）与 op_kernel 的 AscendC 实现（在 Device 上计算），再由 op_api 的 aclnn 接口把三者串起来。def 是「户口本」，不是「发动机」。

**练习 2**：ScatterBlockUpdate 的输出为什么也叫 `input`？

**参考答案**：它是原地更新算子——把 `update` 的内容按 `indices` 写回 `input` 本身。输出声明为 `input` 是向框架声明「输入 input 会被修改并作为结果输出」，与 u1-l3 讲过的 `executor->CreateView`（保留 stride 的视图）配合使用。

**练习 3**：类名 `AiInfraScatterBlockUpdate` 与目录名 `ai_infra_scatter_block_update` 是什么关系？`build.sh -n` 应该传哪个？

**参考答案**：同一个算子的两种拼写约定——类名是大驼峰的算子类型名（注册表与 `l0op::AiInfraScatterBlockUpdate`、`aclnnAiInfraScatterBlockUpdate` 接口名用它），目录名/文件名是小写下划线（构建系统用它）。`build.sh -n` 传的是目录名形式（`ai_infra_scatter_block_update`），因为构建系统按目录名过滤（见 4.4.3 的 `func.cmake`）。

### 4.2 Input / Output / Attr：参数类型与 DataType/Format 组合

#### 4.2.1 概念说明

**ParamType（参数性质）**只有三种取值：

| 取值 | 含义 | 仓内例子 |
| --- | --- | --- |
| `REQUIRED` | 调用时必须传 | scatter 的 `input`/`indices`/`update` |
| `OPTIONAL` | 可以不传（常配合空指针/空张量判断） | conv1d 的 `bias`、`query_start_loc` 等 |
| `DYNAMIC` | 动态输入（个数可变的输入组） | gqa 的 `key`/`value` |

**DataType / Format / UnknownShapeFormat 三个列表**：这是本讲最容易看错的地方。它们不是「集合」，而是**等长的组合表**——三个列表按下标一一对齐，第 i 个位置共同描述「第 i 种被支持的类型方案」：

\[ N = |\mathrm{DataType}| = |\mathrm{Format}| = |\mathrm{UnknownShapeFormat}| \]

即列表长度必须相等，算子共支持 \( N \) 种 (数据类型, 格式) 组合。当算子有多个输入时，**各输入的组合表也按同一下标对齐**：组合编号 i 下，每个输入取自己列表的第 i 个类型。`UnknownShapeFormat` 则声明「当该维度 shape 未知（-1）时使用什么格式」。

其他几个链式方法（gqa 中出现）：

- `AutoContiguous()`：声明框架在该输入进入算子前自动做连续化（对应 u1-l3 提过的非连续输入处理）。
- `ValueDepend(OPTIONAL)`：标记该输入的**数值**（而不只是形状）会在 host 侧被使用——例如 `actual_seq_lengths` 这类影响 tiling 决策的小张量，tiling 时要读它的实际值。
- `DataTypeList(...)` / `FormatList(...)`：列表形式的变体写法，语义同样是声明「该输入支持的类型/格式集合」，仓内与 `DataType`/`Format` 混用；两者精确差别定义在 CANN 头文件中（**待确认**），初学阶段按「等价的列表式声明」理解即可。

**Attr（标量属性）**：Input/Output 是张量，Attr 是编译期/调用期传入的标量配置（Int/Float/String/Bool），写法是 `this->Attr("名字").AttrType(REQUIRED/OPTIONAL).Int(默认值)`——给了默认值的 Attr 即使不传也有取值。

#### 4.2.2 核心流程

以 scatter 的 `input` 与 `indices` 两个输入为例拆解组合表。`input` 的 DataType 列表是 4 种类型重复两遍（共 8 项），`indices` 是 4 个 INT32 接 4 个 INT64（共 8 项），Format/UnknownShapeFormat 全是 FORMAT_ND（各 8 项）。按下标对齐后得到 8 种合法组合：

| 组合编号 | input 类型 | indices 类型 | 格式 |
| --- | --- | --- | --- |
| 0 | BF16 | INT32 | ND |
| 1 | FLOAT16 | INT32 | ND |
| 2 | FLOAT | INT32 | ND |
| 3 | INT8 | INT32 | ND |
| 4 | BF16 | INT64 | ND |
| 5 | FLOAT16 | INT64 | ND |
| 6 | FLOAT | INT64 | ND |
| 7 | INT8 | INT64 | ND |

调用方传入 `(input=BF16, indices=INT64)` 时框架命中组合 4；若传 `(input=BF16, indices=INT8)` 则没有任何组合匹配，会在校验阶段被拒绝。`input` 列表「重复两遍」正是为了让它与 `indices` 的 8 项保持等长对齐。

运行期匹配流程：

```text
调用 aclnnXxx(x, indices, update, ...)
   │
   ▼
框架从注册表取出该算子的原型（def 声明）
   │
   ▼
遍历组合表：逐组合检查每个输入的实际 dtype 是否等于组合中声明的类型
   │ 命中唯一组合 → 通过；无命中 → 参数校验失败
   ▼
进入 tiling 与 kernel 下发
```

#### 4.2.3 源码精读

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:30-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L30-L37) —— `indices` 输入：REQUIRED，DataType 是 `{INT32×4, INT64×4}` 的 8 项列表，Format/UnknownShapeFormat 各 8 项 FORMAT_ND。与上面的组合表逐字对应。

[ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp:23-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L23-L37) —— conv1d 的必选输入写法：每项 2 个组合（BF16/FP16 配 FORMAT_ND）。这是「小组合表」的标准长相。

[ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp:38-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L38-L42) —— OPTIONAL 输入的例子：`query_start_loc` 标记为 `ParamType(OPTIONAL)`，不传时 op_api 层会走空指针分支（这正是 u1-l3 讲过的「参数三步检查」要配合的东西）。

[ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp:93-100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L93-L100) —— Attr 声明：9 个整型/布尔属性全部 REQUIRED，如 `run_mode`（0: prefill，1: decode）、`block_size`、`inplace`。注释直接解释了每个属性的取值含义——**Attr 就是算子的「旋钮」**，同一段 kernel 代码靠不同属性值切换行为。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:23-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L23-L42) —— gqa 的三种新写法同屏出现：`query` 用 `FormatList({ge::FORMAT_ND})` + `AutoContiguous()`；`key`/`value` 用 `ParamType(DYNAMIC)`（动态输入）；`sparse_indices` 用 `DataTypeList({ge::DT_INT32})`（单类型列表）。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:53-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L53-L58) —— `actual_seq_lengths` 声明了 `ValueDepend(OPTIONAL)`：标记它的数值会被 host 侧消费（真实序列长度影响 tiling 切分），框架需保证值可用。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:188-192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L188-L192) —— 多输出写法：`attention_out`（FP16/BF16）与 `softmax_lse`（FLOAT）两个输出各自独立声明，也演示了「单行链式压缩写法」：`this->Output("softmax_lse").ParamType(REQUIRED).DataTypeList({ge::DT_FLOAT}).FormatList({ge::FORMAT_ND});`。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:193-210](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L193-L210) —— 带默认值的 Attr：`scale` 默认 1.0、`input_layout` 默认 `"BSH"`、`softmax_lse_flag` 默认 `false` 等。对比 conv1d 的全 REQUIRED 无默认值写法，可以看出 Attr 越丰富的算子越倾向给默认值以简化调用。

#### 4.2.4 代码实践

**实践目标**：学会「数组合」——从一个 def 文件反推它支持的类型组合。

**操作步骤**：

1. 打开 [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:38-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L38-L45)，数出 `update` 输入三个列表的长度。
2. 把 `update` 加进 4.2.2 的组合表（它应与 `input` 完全同构：8 项、BF16/FP16/FLOAT/INT8×2）。
3. 回答：调用方传 `(input=FLOAT16, indices=INT32, update=BF16)` 是否合法？
4. 再看 conv1d 的 `x` 与 `weight`（[ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp:23-32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L23-L32)），回答：`x` 用 FP16 而 `weight` 用 BF16 是否合法？

**需要观察的现象**：`update` 的三列表长度与 `input` 相同且逐项相同；conv1d 两个输入的列表都是 2 项。

**预期结果**：scatter 的 `update` 组合表与 `input` 完全一致（同为 8 组合）。`(FP16, INT32, BF16)` **不合法**——按下标对齐后没有任何一个编号同时满足三个输入的实际类型（`input` 命中组合 1 要求 `update` 也是 FP16）。conv1d 中 `x=FP16, weight=BF16` 同理**不合法**（组合 0 要求同为 BF16，组合 1 要求同为 FP16）。这就是「组合对齐」语义的直接推论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 scatter 的 `input` 要把 4 种 dtype 写两遍、凑成 8 项？

**参考答案**：因为 `indices` 支持 INT32/INT64 两种类型，交叉后共 8 种组合；组合表要求所有输入的三列表等长且按下标对齐，所以 `input` 必须把自己的类型列表扩到 8 项（每类型出现两次）与 `indices` 的 8 项对齐。

**练习 2**：`Format({FORMAT_ND, FORMAT_ND})` 与 `FormatList({FORMAT_ND})` 有什么直观差别？

**参考答案**：在 conv1d 这类「每种 dtype 配一个格式」的写法里，`Format` 列表长度必须等于 DataType 列表长度（2 项）；`FormatList({FORMAT_ND})` 是 gqa 采用的列表式变体，只用一个元素声明所有组合共用 ND 格式。两者都是声明「支持的格式集合」，精确差别定义在 CANN 头文件中（待确认），初学阶段按等价理解。

**练习 3**：`ValueDepend` 与 `ParamType(OPTIONAL)` 会不会同时出现在一个输入上？为什么？

**参考答案**：会。gqa 的 `actual_seq_lengths` 就是 `ParamType(OPTIONAL)` + `ValueDepend(OPTIONAL)`（[源码 L53-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L53-L58)）。二者维度不同：前者说「这个张量可以不传」，后者说「传了的话它的数值（而非仅形状）会被 host 侧使用」。

### 4.3 OpAICoreConfig 与 SOC 配置

#### 4.3.1 概念说明

`OpAICoreConfig` 描述「这个算子在 AI Core 上的能力与编译行为」，通过 `this->AICore().AddConfig("<soc>", config)` 绑定到具体 SOC 版本。同一个算子可以对不同 SOC 登记不同配置（本仓库三个标本都是两种 SOC 共用同一份配置）。

仓内反复出现的开关及工程含义（按仓内一致用法归纳；精确定义在 CANN 头文件中）：

| 开关 | 仓内统一取值 | 工程含义 |
| --- | --- | --- |
| `DynamicCompileStaticFlag` | true | 以「动态 shape 方式」编译出一份二进制，静态 shape 网络也复用它 |
| `DynamicFormatFlag` | true | 支持动态格式选择（配合 DataType/Format 多组合声明） |
| `DynamicRankSupportFlag` | true | 支持维数（rank）可变的输入 |
| `DynamicShapeSupportFlag` | true | 支持动态 shape（维度为 -1，运行时才确定） |
| `NeedCheckSupportFlag` | false | 执行前无需再调「当前输入是否支持」的检查（实现声明覆盖所有已声明组合） |
| `PrecisionReduceFlag` | 仅 gqa 为 true | 允许框架在特定场景使用降精度实现 |
| `ExtendCfgInfo(k, v)` | 见下 | 键值对形式的扩展配置 |

`ExtendCfgInfo` 的两个仓内实例：

- conv1d：`("opFile.value", "ai_infra_fused_causal_conv1d")` —— 指向算子实现文件的标识。
- gqa：`("aclnnSupport.value", "support_aclnn")` 声明支持 aclnn 直调；`("jitCompile.flag", "static_false,dynamic_false")` 关闭即时编译（JIT），即静态/动态场景都不在线编译，使用预编译二进制——大算子编译耗时长，关 JIT 换取部署确定性。

#### 4.3.2 核心流程

SOC 配置在两个时刻起作用：

```text
编译期：编译系统读取 AddConfig 登记的 SOC 列表
        → 只为列表中的 SOC 生成/打包二进制
        （build.sh -c ascend910_93 的产物里不会包含未登记 SOC 的实现）

运行期：aclnn 执行器按当前硬件 SOC 版本查配置
        → 依据动态开关决定是否做支持性检查、格式选择、精度策略
```

因此「def 里没登记的 SOC」与「build.sh 没编的 SOC」都会导致算子在该硬件上不可用——前者是登记问题，后者是构建问题（u1-l2 的 `-c` 参数）。

#### 4.3.3 源码精读

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:56-63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L56-L63) —— 最小 SOC 配置：一个 `OpAICoreConfig` 局部对象链式打开 4 个动态开关、关掉支持检查，随后两次 `AddConfig` 分别登记 `ascend910b` 与 `ascend910_93`。

[ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp:102-110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_def.cpp#L102-L110) —— 在同样四个开关之外多了 `.ExtendCfgInfo("opFile.value", "ai_infra_fused_causal_conv1d")`，把算子与其实现文件标识绑定，同样登记两种 SOC。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:211-221](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L211-L221) —— 大算子的完整配置：在五个基础开关之上追加 `PrecisionReduceFlag(true)`（允许降精度）和两条 `ExtendCfgInfo`（声明支持 aclnn 直调、关闭静态/动态 JIT），注释 `// use 910B` 表明 910b 是主要目标硬件。

#### 4.3.4 代码实践

**实践目标**：体会「为什么大算子要关 JIT」。

**操作步骤**：

1. 对比三个标本的 `OpAICoreConfig` 段，制作差异表（开关名 × 三个算子）。
2. 用 `Grep` 在 `ascendc/src` 下搜索 `jitCompile.flag`，统计有多少个 def 文件设置了它、取值是什么。
3. 思考并写下：若把 gqa 的 `NeedCheckSupportFlag` 改为 true，运行期会多出哪一步行为？

**需要观察的现象**：`jitCompile.flag` 只出现在少数几个大型注意力算子的 def 中；绝大多数算子只用五个基础开关。

**预期结果**：差异表显示三个算子的基础开关完全一致，差别集中在 `PrecisionReduceFlag` 与 `ExtendCfgInfo`；`NeedCheckSupportFlag(true)` 意味着执行前要先跑一次「当前输入组合是否被支持」的检查（多一次 host 侧开销，但更保险）。本实践为源码阅读型，行为差异**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：想让一个算子额外支持 `ascend950`，def 文件里改哪里就够了吗？

**参考答案**：不够。def 里加一行 `AddConfig("ascend950", config)` 只是「登记」；还需要 `build.sh -c` 指定对应 SOC 目标完成编译，并确保 op_kernel 实现适配了该芯片的硬件特性（如核数、UB 大小）。登记、编译、实现三者齐备才真正可用。

**练习 2**：`DynamicFormatFlag(true)` 与 4.2 节的 DataType/Format 多组合声明是什么关系？

**参考答案**：多组合声明（如 scatter 的 8 组合）只是「登记了多种可选方案」；`DynamicFormatFlag(true)` 是告诉框架这个算子具备在运行期根据实际输入选择格式方案的能力，二者一个提供「选项表」、一个打开「选择机制」，配合使用。

**练习 3**：为什么 `NeedCheckSupportFlag` 仓内统一设为 false？

**参考答案**：这些融合算子的 AscendC 实现按动态 shape 方式编写，声明即全量支持（四种动态开关全开），无需在每次执行前再做一次支持性检查，省掉 host 侧开销。反过来，若某个算子只支持有限 shape 范围，就应该打开这个开关让框架先检查再执行。

### 4.4 从 OP_ADD 到 cust_opsproto_rt2.0.so：算子如何被发现

#### 4.4.1 概念说明

「新增一个算子，系统怎么知道它存在？」答案分两层：

1. **编译期发现（仓库自己实现）**：算子目录 + 目录里的 `CMakeLists.txt`。顶层 CMake 用 `file(GLOB ...)` 扫描 `src/ops-transformer/**/**/CMakeLists.txt` 与 `src/ops-nn/**/**/CMakeLists.txt`，目录名即算子名；`build.sh -n` 传入的 `ASCEND_OP_NAME` 在这一层过滤。def.cpp 由算子自己的 CMakeLists 挂进 `op_host_aclnnInner` 目标。
2. **运行期发现（CANN 机制）**：`OP_ADD(类名)` 宏。它来自 CANN 头文件 `register/op_def_registry.h`（仓库内无此文件，宏展开**待确认**），效果是在 `cust_opsproto_rt2.0.so` 被加载时，把 OpDef 子类登记进全局算子注册表，此后框架可按算子名查到原型。

所以实践任务里「OP_ADD 如何让算子被发现」的完整回答是：**OP_ADD 只负责运行期注册；编译期还要把 def.cpp 挂进构建目标，两件事都做，算子才真正可见。**

#### 4.4.2 核心流程

```text
bash build.sh -n 'ai_infra_scatter_block_update' -c ascend910_93
   │  build.sh 把 -n 翻译成 ASCEND_OP_NAME（u1-l2）
   ▼
顶层 CMakeLists:305 调用 op_add_subdirectory(OP_LIST OP_DIR_LIST)
   │  func.cmake:45 GLOB 扫描所有算子目录的 CMakeLists.txt
   │  func.cmake:62-68 目录名不在 ASCEND_OP_NAME 列表 → 跳过
   ▼
逐个 add_subdirectory 进入算子目录（如 scatter 的 CMakeLists）
   │  L19-21: def.cpp → target_sources(op_host_aclnnInner)
   │  L23-25: tiling.cpp → optiling 目标
   │  L38-41: aclnn*.cpp → opapi 目标
   ▼
顶层 CMakeLists:369 收集 op_host_aclnnInner 的源码列表
   │  L409-419: 对以 _def 结尾的源码去掉后缀得算子名
   │            为每个算子生成 inner/aclnnInner_<名>.cpp 与 <名>_proto.cpp/h
   ▼
L479-481: 生成的 *_proto.cpp 编入 opsproto 目标
   │  L196-198: opsproto 产出名为 cust_opsproto_rt2.0.so
   │  L199-201: 安装到 vendors 的 op_proto 目录
   ▼
run 包安装后 so 被框架加载 → OP_ADD 静态注册生效 → 算子可被按名调用
```

（承接 u1-l2 的结论：`cust_opsproto_rt2.0.so` 就是三个产物库中的「算子原型库」。）

#### 4.4.3 源码精读

[ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:19-25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L19-L25) —— 编译期挂接的关键两行：`target_sources(op_host_aclnnInner PRIVATE op_host/ai_infra_scatter_block_update_def.cpp)` 把 def.cpp 加进原型目标；紧接着把 tiling.cpp 加进 `optiling`（即 `cust_opmaster_rt2.0`，见 [ascendc/CMakeLists.txt:247-249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L247-L249) 的 OUTPUT_NAME）。文件头注释还说明了两个入口的取舍：自己实现了 aclnn 接口用 `op_host_aclnnInner`，用自动生成的 aclnn 接口则用 `op_host_aclnn`。

[ascendc/cmake/func.cmake:41-54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L41-L54) —— `op_add_subdirectory` 函数开头：`file(GLOB ...)` 按 `src/ops-transformer/**/**/CMakeLists.txt` 等模式收集所有算子构建脚本，从路径提取算子目录名。**GLOB 意味着「新建算子目录 + CMakeLists.txt」即被自动发现，无需改任何中央清单。**

[ascendc/cmake/func.cmake:62-68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L62-L68) —— `-n` 过滤逻辑：若定义了 `ASCEND_OP_NAME` 且不是 `all`，目录名不在列表中的算子直接 `continue()` 跳过。这就是 `build.sh -n '算子目录名'` 只编译指定算子的实现位置。

[ascendc/CMakeLists.txt:91-97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L91-L97) —— 顶层创建 `op_host_aclnnInner` 共享库目标（`EXCLUDE_FROM_ALL`，说明它更多是「源码收集器」，真正产物靠后续生成流程）。

[ascendc/CMakeLists.txt:409-419](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L409-L419) —— 遍历 `op_host_aclnnInner` 的源码：对以仓库根开头的源文件取 `NAME_WE`，用正则去掉 `_def` 后缀得到算子名，然后登记三个生成文件——`inner/aclnnInner_<算子名>.cpp`（aclnn 包装）、`<算子名>_proto.cpp/h`（原型源码与头）。**注意这里依赖「文件名 = 算子目录名 + `_def`」的命名约定**：新建算子时 def 文件名必须与目录名一致，否则无法被识别。

[ascendc/CMakeLists.txt:479-481](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L479-L481) —— 生成的 `*_proto.cpp` 加入 `opsproto` 目标；结合 [ascendc/CMakeLists.txt:196-201](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L196-L201) 的 `OUTPUT_NAME cust_opsproto_rt2.0` 与安装到 `packages/vendors/<VENDOR>/op_proto/lib/...`，原型信息的最终归宿就是 run 包里的算子原型库。

[ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp:224](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_def.cpp#L224) —— `OP_ADD` 的双参数形式：`OP_ADD(AiInfraSparseFlashAttentionGqa, optiling::SparseFlashAttentionCompileInfo);` 第二参数把一个「编译信息」结构体与算子绑定。该结构体定义在 [ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_compile_info.h:21-31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_compile_info.h#L21-L31)：记录 `aivNum`/`aicNum`（向量核/立方核数量）、`ubSize`/`l1Size`/`l0ASize` 等硬件资源尺寸与 SOC 版本——即「在什么硬件参数下编译」的元信息，tiling 与 UT 都会用到它（UT 中以 `optiling::optilingSfa::SparseFlashAttentionCompileInfo` 形式出现在 [tests/ut/op_host/test_sparse_flash_attention_gqa_tiling.cpp:50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/tests/ut/op_host/test_sparse_flash_attention_gqa_tiling.cpp#L50)）。名字解析细节依赖 CANN 头文件（**待确认**）。

#### 4.4.4 代码实践

**实践目标**：亲眼验证「GLOB + 目录名过滤」的发现机制。

**操作步骤**：

1. 阅读以上 4 段源码，画出从 `build.sh -n` 到 `cust_opsproto_rt2.0.so` 的流程图（可对照 4.4.2 校对）。
2. （有昇腾环境时）执行 `bash build.sh -n 'ai_infra_scatter_block_update' -c ascend910_93`，在 CMake 配置阶段日志中确认只有该算子被加入 `OP_LIST`，产物目录中出现 `cust_opsproto_rt2.0.so`。
3. 换一个不存在的名字 `-n 'no_such_op'` 再跑一次，观察配置阶段的算子列表变化。

**需要观察的现象**：`-n` 指定单算子时构建规模明显小于全量；错误名字导致算子列表为空或构建提前结束。

**预期结果**：与 4.4.2 流程一致；具体日志输出**待本地验证**（无硬件环境时，第 1 步的源码流程图即为交付物）。

#### 4.4.5 小练习与答案

**练习 1**：新建目录 `my_add` 并写好 `my_add_def.cpp`，但忘记写目录里的 `CMakeLists.txt`，会发生什么？

**参考答案**：`op_add_subdirectory` 的 GLOB 模式匹配的是「算子目录下的 CMakeLists.txt」，没有该文件的目录根本不会被收集——算子在编译期不存在，def.cpp 不会被编译，`OP_ADD` 也就无从注册。

**练习 2**：把 def 文件命名为 `myadd_def.cpp` 而目录叫 `my_add`，会有问题吗？

**参考答案**：会。顶层 CMakeLists:414 用「去掉 `_def` 后缀的文件名」作为算子名生成 proto 与 aclnn 包装，`myadd` 与目录名 `my_add` 不一致，会把生成物指向错误的算子名。约定是：**def 文件名 = 目录名 + `_def.cpp`**。

**练习 3**：`OP_ADD` 与 `REGISTER_TILING_DATA_CLASS`（下一讲会讲）是一回事吗？

**参考答案**：不是。`OP_ADD` 注册的是**算子原型**（名字、输入输出、dtype/format、SOC 配置），产出进 `cust_opsproto_rt2.0.so`；`REGISTER_TILING_DATA_CLASS` 注册的是 **tiling 数据结构**（host 与 device 之间传递的「施工图」类型），服务于 optiling 层。二者分别位于 def 与 tiling 文件中，互相独立。

## 5. 综合实践

**任务**：为假想算子 `my_add`（输入 `x`、`y`，输出 `z`，支持 FP16/BF16，逐元素相加）手写一份合规的 `op_host/my_add_def.cpp`，并说清它如何被系统发现。本实践只需源码阅读与编写，无需硬件。

**第 1 步：写 def 文件**。参照 scatter 的模子写出如下内容（**示例代码**，非仓库原有文件；本实践不修改仓库源码，可在仓库外另建目录练手）：

```cpp
// 示例代码：my_add/op_host/my_add_def.cpp
#include "register/op_def_registry.h"

namespace ops {
class MyAdd : public OpDef {
public:
    explicit MyAdd(const char* name) : OpDef(name)
    {
        // 输入 x、y 与输出 z：2 种类型组合（FP16 / BF16），均为 ND 格式
        this->Input("x")
            .ParamType(REQUIRED)
            .DataType({ge::DT_FLOAT16, ge::DT_BF16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND});
        this->Input("y")
            .ParamType(REQUIRED)
            .DataType({ge::DT_FLOAT16, ge::DT_BF16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND});
        this->Output("z")
            .ParamType(REQUIRED)
            .DataType({ge::DT_FLOAT16, ge::DT_BF16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND});

        // SOC 能力配置：照抄仓内统一写法（四个动态开关 + 免支持检查）
        OpAICoreConfig aicConfig;
        aicConfig.DynamicCompileStaticFlag(true)
                .DynamicFormatFlag(true)
                .DynamicRankSupportFlag(true)
                .DynamicShapeSupportFlag(true)
                .NeedCheckSupportFlag(false);
        this->AICore().AddConfig("ascend910b", aicConfig);
        this->AICore().AddConfig("ascend910_93", aicConfig);
    }
};

OP_ADD(MyAdd);

}  // namespace ops
```

自查清单：

- 类名大驼峰 `MyAdd`，文件名 `my_add_def.cpp`，目录名 `my_add`（满足 4.4.3 的命名约定）。
- 三个张量的三列表均等长（2 项），组合按下标对齐：组合 0 = 全 FP16，组合 1 = 全 BF16——`x` 用 FP16、`y` 用 BF16 的混合调用会被拒绝。
- 无 Attr（my_add 没有标量旋钮，可不写 `this->Attr(...)`）。
- `OP_ADD(MyAdd)` 以分号结尾、放在 `namespace ops` 内。

**第 2 步：写挂接脚本**。仿 [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:19-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L19-L21)，在 `my_add/CMakeLists.txt` 中至少写：

```cmake
# 示例代码：my_add/CMakeLists.txt 的关键行（完整算子还需 tiling/kernel 的挂接，见第 6 单元）
target_sources(op_host_aclnnInner PRIVATE
        op_host/my_add_def.cpp
)
```

**第 3 步：回答「OP_ADD 如何让算子被发现」**。用 4.4 的两层机制作答：编译期——`my_add` 目录含 CMakeLists.txt 被 `func.cmake:45` 的 GLOB 扫到，`build.sh -n 'my_add'`（转成 `ASCEND_OP_NAME`）放行该目录，def.cpp 经 `target_sources` 进入 `op_host_aclnnInner`，顶层生成 `MyAdd_proto.cpp` 编入 `opsproto`（产出 `cust_opsproto_rt2.0.so`）；运行期——so 加载时 `OP_ADD(MyAdd)` 的静态注册把原型登记进全局注册表，框架从此认识 `MyAdd` 这个名字。

**第 4 步（可选，有昇腾环境时）**：把目录放进 `src/ops-transformer/index/` 后执行 `bash build.sh -n 'my_add' -c ascend910_93`，观察配置阶段是否列出 `my_add`、是否生成 `cust_opsproto_rt2.0.so`。注意只挂 def 时链接/后续目标可能因缺少 tiling、aclnn 实现而报错——这是预期现象，完整九件套在第 6 单元综合实战补齐；本步**待本地验证**。

**预期结果**：得到一份能通过「对照检查」的 def 文件（与三个标本逐项同构），以及一段准确描述两层发现机制的文字说明。

## 6. 本讲小结

- `*_def.cpp` 用一个继承 `OpDef` 的类给算子登记「身份证」：输入/输出的 ParamType、DataType/Format 组合、Attr 标量属性、OpAICoreConfig 能力开关与 SOC 配置；文件以 `OP_ADD(类名)` 收尾。
- `DataType`/`Format`/`UnknownShapeFormat` 是**等长的组合表**，多个输入按同一组合编号对齐——scatter 的 8 项列表即 `input` 的 4 种类型（BF16/FLOAT16/FLOAT/INT8）与 `indices` 的 INT32/INT64 交叉出的 8 种方案。
- `ParamType` 有 REQUIRED / OPTIONAL / DYNAMIC 三种；gqa 还示范了 `AutoContiguous`（自动连续化）、`ValueDepend`（值依赖）与 `DataTypeList/FormatList` 变体写法。
- `OpAICoreConfig` 的四个动态开关（shape/rank/format/编译方式）是仓内标配；`ExtendCfgInfo` 可附加 `jitCompile.flag`（关 JIT）、`opFile.value` 等扩展配置；`AddConfig` 把配置登记到 `ascend910b`/`ascend910_93` 等具体 SOC。
- 算子「被发现」是两层机制：编译期靠算子目录 + CMakeLists 被 `op_add_subdirectory` GLOB 收集并按 `-n` 过滤，def.cpp 编入 `cust_opsproto_rt2.0.so`；运行期靠 `OP_ADD` 静态注册进全局注册表。
- 命名约定是硬约束：目录名 = def 文件名去掉 `_def`，类名是其大驼峰形式；违反约定会导致 proto 生成错位。

## 7. 下一步学习建议

原型登记好之后，框架还不知道「怎么校验参数并下发执行」。下一讲 **u2-l2《op_api 层：aclnn 接口的两段式设计》** 将精读 `aclnn_ai_infra_scatter_block_update.cpp`，看 GetWorkspaceSize 与执行函数如何配合本讲的 def 声明做参数三步检查。随后 **u2-l3《TilingData 与 TilingBaseClass 七步框架》** 讲 host 侧切分。继续阅读建议：把 gqa 的 def 从头到尾再读一遍并数出它声明的类型组合数；对照阅读 [ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs) 下的接口文档，体会「文档中的函数签名 ↔ def 中的 Input/Attr 声明」的一一对应关系。
