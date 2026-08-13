# 算子交付件与配置体系

## 1. 本讲目标

上一讲（u6-l3）我们把一个自定义算子的「可执行代码」接进了 ATB：写好了高层 `Operation`、`OpsRunner`，并用 `CreateOperation`/`REG_RUNNER_TYPE`/`REG_OP_PARAM` 完成了注册。但仅靠这些代码，算子还无法被测试框架识别、无法对外发布规格、也无法参与自动化的精度/性能回归。

本讲聚焦「代码之外的交付件」——当一个新算子真正要交付时，除了 `.cpp`/`.h`，你还要改/加哪些**配置与描述文件**。学完后你应当能够：

1. 背出一个新算子完整的交付件清单（哪些文件新建、哪些文件修改）。
2. 看懂 `ops_configs/atb_ops_info.ini` 里 `[Op] + input/output.dtype/format` 的语法，并解释它是如何被运行时用来校验「实际张量是否落在算子声明的规格组合内」的。
3. 理解 Param 的「JSON 化」：`OpParamToJson` 模板特化做什么、`rsv` 预留字段为何被刻意排除在 JSON 之外。
4. 理解测试框架如何把一段 JSON 文本反序列化成 `Param` 并创建算子（`operation_funcs.cpp` 的 `g_funcMap` 机制），以及 `op_list.yaml` 算子清单在 Kernel 构建中的作用。

## 2. 前置知识

在进入配置体系前，请确认你已理解以下来自前置讲义的概念（本讲只复用、不重复展开）：

- **Param 骨架与 `rsv` 闸门**（u2-l3、u6-l3）：每个推理算子的 `XxxParam` 都是「带默认值的 POD 字段 + 末尾 `uint8_t rsv[N] = {0}`」。`rsv` 必须全 0，`OPERATION_PARAM_FUNCS` 宏在创建/更新算子时逐字节校验，非 0 即返回 `ERROR_INVALID_PARAM`，它是**版本兼容闸门**。
- **两段式执行 Setup/Execute 与 `OperationBase` 钩子**（u1-l6、u3-l1、u6-l3）：高层 `Operation` 继承 `OperationBase`，`Setup` 里会做一次「输入张量描述符是否合法」的校验，本讲的 ini 校验正是嵌入在这条校验链里。
- **注册名一致铁律**（u6-l3）：算子的名字字符串在「高层 Operation 名 / Runner `opDesc` 字符串 / MKI 注册名」多处必须一致；本讲会再加两处一致点：**ini 段名（IR key）**与**测试 `g_funcMap` 的键**。
- **JSON（nlohmann::json）**：ATB 用 `nlohmann::json` 作为 Param 的序列化中间格式，既用于图信息上报，也用于测试用例驱动。

一句话定位：u6-l3 解决「算子能跑」，本讲解决「算子能被校验、能被测试、能被交付」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来看什么 |
|------|------|----------------|
| `docs/starting_from_a_simple_operator.md` | 官方「从零开发一个 Add 算子」教程 | 它逐条列出了新算子的**全部交付件**，是本讲清单的事实来源 |
| `ops_configs/atb_ops_info.ini` | 全量算子规格描述（约 7200 行） | 学习 ini 段的 `dtype/format` 组合语法 |
| `include/atb/infer_op_params.h` | 所有推理算子 `XxxParam` 定义 | 看 Param 骨架与 `rsv` 字段（以 `LinearParam` 为例） |
| `src/atb/utils/param_to_json.cpp` / `.h` | Param → JSON 的模板特化集合 | 学习 `OpParamToJson` 的写法 |
| `src/atb/operation/atb_operation_ir_cfg.cpp` | 加载 ini 的单例配置 | ini 文件从磁盘到内存的加载点 |
| `src/atb/operation/operation_base.cpp` | `OperationBase` 实现 | `CheckIniMatch`：ini 规格如何校验实际张量 |
| `src/atb/operation/op_param_funcs.h` | `OPERATION_PARAM_FUNCS` / `OP_PARAM_RSV_CHECK` 宏 | `rsv` 闸门与工厂三件套 |
| `tests/framework/c++/atb_torch/operation/operation_funcs.cpp` | 测试框架的算子反序列化工厂 | `g_funcMap`、`XxxOperationCreate`、JSON 驱动测试入口 |
| `src/ops/ops_infer/linear/linear_operation.cpp` | Linear 算子 | 看 ini 的「IR key」如何由 Param 字段**动态拼接** |
| `src/kernels/kernels/CMakeLists.txt` | Kernel 层构建 | `op_list.yaml` 算子清单的自动生成 |
| `ops_customize/customize_ops_configs/customize_ops_info.ini`、`ops_customize/include/customize_op_params.h` | `ops_customize` 独立开发分支的平行配置 | 对照主仓流程的「轻量版」 |

## 4. 核心概念与源码讲解

### 4.1 新算子交付件全景图

#### 4.1.1 概念说明

「交付件（deliverables）」指的是：让一个新算子从「在我机器上能编译」升级为「能进 ATB 主仓、被 CI 测试、对外发布规格、跨芯片可用」所必须提交的全部产物。

u6-l2（Kernel）和 u6-l3（框架集成）覆盖的是**代码交付件**——`.cpp` 里真正执行计算的逻辑。本讲的 4.1 先给一张「全景图」，把代码交付件和**配置交付件**摆在一起，让你看清边界；随后 4.2~4.4 再分别深入三块配置。

一句话：**代码交付件让算子能算出结果，配置交付件让算子能被信任（规格校验）、能被复现（JSON 序列化）、能被验证（测试反序列化）、能被构建（算子清单）。**

#### 4.1.2 核心流程：交付件清单

官方教程以一个最简单的 `Addcustom`（两个 int32 张量相加）为例，逐条列出了新算子的交付件。把它们按「新建 / 修改」和「代码 / 配置」两个维度归类如下：

**A. 代码交付件（u6-l2、u6-l3 已讲，此处仅列位）**

- 新建 `src/kernels/kernels/<op>/`：Kernel 四件套（`op_kernel/*.cpp`、`tiling/*`、`<op>_kernel.cpp`、`<op>_operation.cpp`、`CMakeLists.txt`）。
- 新建 `src/kernels/include/asdops/params/<op>.h`：MKI 层 `OpParam::<Op>` 结构。
- 新建 `src/ops/ops_infer/<op>/`：高层 `Operation` 与 `OpsRunner`。
- 注册：`REG_KERNEL_BASE` / `REG_OPERATION` / `REG_RUNNER_TYPE` / `REG_OP_PARAM`。

**B. 配置交付件（本讲重点）**

1. 在 `include/atb/infer_op_params.h` 增加 `infer::XxxParam`（**含 `rsv`**）。
2. 在 `src/kernels/include/asdops/params/params.h` 增加一行 `#include`（聚合 MKI 层 Param 头）。
3. 在 `src/kernels/configs/kernels/op_list.yaml`（首次编译后生成）登记算子→芯片支持关系。
4. 在 `ops_configs/atb_ops_info.ini` 增加一个 `[XxxOperation]` 段，声明输入输出 dtype/format 规格。
5. 在 `src/atb/utils/param_to_json.cpp` 增加 `OpParamToJson` 的模板特化（Param→JSON）。
6. 在 `tests/framework/c++/atb_torch/operation/operation_funcs.cpp` 增加 `XxxOperationCreate` 反序列化函数，并把 `{"XxxOperation", &XxxOperationCreate}` 登记进 `g_funcMap`。

伪代码表达整体关系：

```text
Param 结构 (infer_op_params.h)
   │
   ├──> ini (atb_ops_info.ini)        ──运行时──> CheckIniMatch 校验 dtype/format
   ├──> param_to_json.cpp             ──序列化──> JSON（图上报 / 测试输入）
   ├──> operation_funcs.cpp           ──反序列化──> JSON -> Param -> CreateOperation（测试）
   └──> op_list.yaml                  ──构建期──> 决定哪些芯片编译该 Kernel
```

#### 4.1.3 源码精读：官方教程的交付件清单

官方教程把上述清单写得非常直白。先看「修改文件」总览段，它一次性点出了四块配置：

[docs/starting_from_a_simple_operator.md:73-96](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L73-L96)：列出要修改的四个文件——`params.h` 聚合、`op_list.yaml` 登记、`infer_op_params.h` 加 Param、（随后还有 ini 与 param_to_json）。

其中 `infer_op_params.h` 里新增的 `AddcustomParam` 是本讲的「最小完整样本」，注意它的 `rsv[12]`：

```c++
struct AddcustomParam {
    int addcustomDim = 0;
    uint8_t rsv[12] = {0};
};
```

[docs/starting_from_a_simple_operator.md:89-96](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L89-L96)：Param 定义骨架——业务字段在前，`rsv` 收尾。

`rsv` 的长度（这里是 12）是**任意非 0 的预留字节数**，不同算子不同（`LinearParam` 是 21，`BlockCopyParam` 是 16），唯一要求是「初值全 0」。它的作用见 4.1.2：版本兼容闸门。

#### 4.1.4 代码实践

> **实践目标**：建立交付件的「全局心智地图」，能在仓库里亲手找到每一类交付件的落点。

1. 用 `Glob`/`grep` 在仓库里定位一个真实算子（例如 `activation`）的六类配置交付件分别在哪里。
2. 填写下面这张表（待本地验证/手工查阅）：

| 交付件 | 文件路径 | activation 对应内容 |
|--------|----------|---------------------|
| Param 定义 | `include/atb/infer_op_params.h` | `ActivationParam` |
| ini 段 | `ops_configs/atb_ops_info.ini` | `[ActivationOperationGELU]` 等 |
| param_to_json | `src/atb/utils/param_to_json.cpp` | `OpParamToJson(const infer::ActivationParam&)` |
| 测试反序列化 | `tests/.../operation_funcs.cpp` | `ActivationOperationCreate` + `g_funcMap` 项 |
| op_list.yaml | `src/kernels/configs/kernels/op_list.yaml`（编译后生成） | `ActivationOperation:` 段 |
| MKI 层 Param | `src/kernels/include/asdops/params/*.h` | activation 对应头 |

3. **需要观察的现象**：你会发现一个算子「横切」在 6 个不同目录里，这正是 u1-l2 讲过的「一个算子分布在多目录」在交付件层面的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AddcustomParam` 的业务字段 `addcustomDim` 必须写在 `rsv` **之前**，能不能交换顺序？

> **答案**：`rsv` 是版本兼容闸门，工厂宏用 `for (uint8_t i : opParam.rsv)` 逐字节扫描它（见 4.3.3）。更重要的是，Param 是 POD 序列化对象，字段顺序决定了二进制布局与 JSON 化时的字段集合；`rsv` 约定**永远在结构末尾**，这样未来向前兼容地新增业务字段时，只需在 `rsv` 前插入并把 `rsv` 长度调整即可，不会破坏老调用方对已有字段偏移的依赖。所以不能交换。

**练习 2**：如果只写完了代码交付件（Kernel + Operation + Runner），但漏掉了 ini 段，算子在 `Setup` 阶段会发生什么？

> **答案**：构造时 `GetOperationIr("XxxOperation")` 取不到段，`operationIr_` 为 `nullptr`。从 4.2.3 的 `CheckIniMatch` 可见，`operationIr_` 为空时函数**直接 return true**（视为无规格约束、放行）。所以「漏写 ini」不会报错，但意味着该算子**彻底失去了 dtype/format 合法性校验**——任何非法类型都会被默默放进 Kernel，等运行到设备侧才出错。这正是 ini 必须交付的根本原因。

---

### 4.2 ini 规格约束（atb_ops_info.ini）

#### 4.2.1 概念说明

`ops_configs/atb_ops_info.ini` 是 ATB 的**全量算子规格说明书**：对每个算子，它声明「合法的输入输出张量组合」。注意它**只描述 dtype（数据类型）和 format（排布）**，不描述 shape——shape 的合法性由各算子自己的 `InferShapeImpl`/`SetupCheckImpl` 用代码判断（见 u4 系列）。

把它和 Param 对照看：
- **Param**（`infer_op_params.h`）描述算子的**行为参数**（转不转置、叠不叠加 bias……）。
- **ini**（`atb_ops_info.ini`）描述算子的**张量规格**（接受哪些 dtype/format 组合）。

二者正交，共同界定「一个算子在什么输入下是合法的」。

#### 4.2.2 核心流程：逗号并列 = 多个「支持组合」

ini 的语法关键是**逗号分隔的并列列表表示多个等价的支持组合（support combination）**，且同一组合内多个张量的位置是对齐（zip）的。

看一个最简单的单输入激活算子：

```ini
[ActivationOperationRELU]
input0.name=x
input0.dtype=float,bf16        ;  2 个组合：(float,nd) 和 (bf16,nd)
input0.format=nd,nd
output0.name=y
output0.dtype=float,bf16
output0.format=nd,nd
```

`dtype` 和 `format` 的逗号数必须相等，第 `i` 个 dtype 配第 `i` 个 format，构成第 `i` 个组合。运行时校验逻辑（`CheckIniMatch`）会遍历这些组合，**只要有一个组合能让所有张量同时匹配**就通过，否则返回 `ERROR_INVALID_TENSOR_INI_MATCH`。

校验流程用伪代码表示：

```text
对每个 supportIdx in 0..supportSize:
    若【所有非空张量】的 dtype == 该组合声明的 dtype
       且 format == 该组合声明的 format:
        则校验通过，返回 true
全部组合都不匹配 -> 返回 false（上报 ERROR_INVALID_TENSOR_INI_MATCH）
```

多输入时，要求**同一 supportIdx 下所有输入、所有输出同时命中**，这是「组合」而非「逐张量独立」的语义——例如一个要求 `x:fp16` 且 `weight:int8` 同时成立的量化算子，会把这两条写在同一个逗号位置。

#### 4.2.3 源码精读

**(1) ini 段的写法**。先看一个干净的双输入通信算子段，注意 7 个 dtype 对应 7 个组合：

[ops_configs/atb_ops_info.ini:104-110](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_configs/atb_ops_info.ini#L104-L110)：`[AllReduceOperation]` 段，`input0.dtype=float16,float,int8,int16,int32,int64,bf16` 与 `output0.dtype=...` 一一并列，共 7 个支持组合。

**(2) ini 如何被加载**。ini 不是写完就扔的文档，它在运行时被读进内存。加载点是 `AtbOperationIrCfg` 单例，它在构造时按 `ATB_HOME_PATH` 找到安装目录下的 `configs/ops_configs/atb_ops_info.ini` 并 `Load`：

[src/atb/operation/atb_operation_ir_cfg.cpp:27-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/atb_operation_ir_cfg.cpp#L27-L41)：`InitOperationIrCfg` 拼出 ini 路径并 `opIrCfg_.Load(...)`；`GetOperationIr(opKey)` 按段名取出某个算子的规格对象 `Mki::OperationIr*`。

注意路径：源码里的 `ops_configs/atb_ops_info.ini` 在安装（`build.sh` 的 install 步骤）后会被拷到 `output/atb/configs/ops_configs/atb_ops_info.ini`，所以运行时读的是**安装产物**，改完 ini 必须重新安装才生效。

**(3) 规格如何校验实际张量**。`CheckIniMatch` 是把「ini 声明」和「用户实际传入的张量」对账的函数。先看它对一个 `supportIdx` 的逐张量比对：

[src/atb/operation/operation_base.cpp:212-227](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L212-L227)：`CheckIniMatchSupportIdx`——遍历张量，**空张量跳过**（这就是 ini 里某些「可空」输入能省略校验的原因），其余张量用 `supportedDtypes[supportIdx]`/`supportedFormats[supportIdx]` 比对 dtype 与 format。

再看外层如何枚举组合并嵌入校验链：

[src/atb/operation/operation_base.cpp:229-252](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L229-L252)：`CheckIniMatch(inTensorDescs)`——若 `operationIr_` 为空直接放行（呼应 4.1.5 练习 2）；否则要求张量个数与声明一致，再 `for supportIdx` 逐组合试探，命中即返回 true。

这个函数在 `InferShapeCheck`（`Setup` 前半段，只看描述符）和 `Execute` 前的检查里都会被调用。失败时的错误信息非常友好，会把实际 dtype/format 与全部支持组合都打印出来（见同文件 L303-L316 的 `GetCombString()` 上报）。

**(4) 进阶：ini 段名（IR key）可以是动态拼接的**。前面 `Addcustom` 的段名就是算子名 `AddcustomOperation`，看似一一对应。但对于行为复杂的算子（如 Linear），一个算子会对应**几十个 ini 段**，段名由 Param 字段 + 芯片类型**运行时拼接**而成：

[src/ops/ops_infer/linear/linear_operation.cpp:310-338](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L310-L338)：构造 `opIrKey`，依次拼入 `Matmul` + (`EinSum`/`WithBias`/`Dequant`…) + (`Float16`/`Bf16`) + (`Atlas800IA2`/`NotAtlas800IA2`)，最终 `GetOperationIr(opIrKey.str())` 取到对应段（如 `LinearOperationMatmulWithBiasAtlas800IA2`）。

这就是为什么 ini 里会有 `[LinearOperationMatmul...]`、`[LinearOperationMatmulDequantFloat16Ascend950]` 一长串段——它们是同一个 `LinearOperation` 在「不同 Param 组合 × 不同芯片」下的规格分身。**结论：ini 段名 = 一个由算子自行决定的 IR key，简单算子用静态名，复杂算子用动态拼接名，校验逻辑完全相同。**

#### 4.2.4 代码实践

> **实践目标**：亲手读一个真实算子的 ini 规格，推断它支持的组合，并用校验语义解释一个「故意传错类型」的失败场景。

1. 打开 `ops_configs/atb_ops_info.ini`，定位 `[AllReduceOperation]` 段（L104 附近）。
2. 数出它声明了几个 dtype 组合（答案：7 个：float16/float/int8/int16/int32/int64/bf16）。
3. **阅读型推断**：假设你给 `AllReduceOperation` 传入一个 `ACL_UINT8` 的张量，对照 4.2.3 的 `CheckIniMatch` 逻辑推断结果。
4. **需要观察的现象（待本地验证）**：预期 `Setup` 返回 `ERROR_INVALID_TENSOR_INI_MATCH`，且日志（`operation_base.cpp` L312-L314）会打印 `CheckIniMatch Failed! Actual Inputs: ...` 与 `Supported Combs: ...`，把 `uint8` 不在支持列表里这一事实直接报给你。
5. **预期结果**：理解「ini 是一道运行时的、基于组合的 dtype/format 闸门」，且报错信息可直接拿来对照 ini 修正输入。

#### 4.2.5 小练习与答案

**练习 1**：某算子 ini 写成 `input0.dtype=float16,int8` 但 `input0.format=nd`（只有 1 个值），会发生什么？

> **答案**：dtype 与 format 的逗号数不一致，属于 ini 本身配置错误。MKI 的 `OperationIr::Load` 在解析时会按 dtype 数建立 `supportSize`，format 不足会导致取 `supportedFormats[idx]` 时越界或取不到值。规范写法是 dtype 有 N 个、format 也写 N 个（即便全是 `nd` 也要 `nd,nd,...,nd`）。这也是为什么 L77-L94 的 `AllGatherVOperation` 段里每个 `format` 都老老实实写了 7 个 `nd`。

**练习 2**：为什么 `CheckIniMatchSupportIdx` 要对「空张量」`continue` 跳过？

> **答案**：ATB 里部分输入是**可选/条件性**的（例如某些量化 scale/offset、或通信算子的辅助张量），用户可能传入一个 `deviceData == nullptr` 的空张量占位。空张量没有真实 dtype 可比，强行校验会误伤合法调用；故约定空张量不参与规格匹配，只校验非空张量。这要求 ini 的设计者在声明规格时也按「非空张量集合」对齐组合。

---

### 4.3 Param 的 JSON 化（param_to_json）

#### 4.3.1 概念说明

「JSON 化」指把一个 C++ `Param` 结构转成 `nlohmann::json` 对象。它有两个真实用途：

1. **图信息上报**：图算子（u5-l2/u5-l3）需要把内部各节点算子的参数序列化成 JSON，用于图结构 dump、IR 上报、调试。`OperationBase::GetParamJson()` 钩子就是入口。
2. **测试与持久化的中间格式**：Param 经 JSON 化后可写入文件；反过来测试用例也以 JSON 文本给出（见 4.4）。JSON 是 Param 的「文本投影」。

ATB 用一个**函数模板** `OpParamToJson<T>` 统一这件事：声明一次，每个 Param 类型写一份**全特化**。

#### 4.3.2 核心流程：模板声明 + 逐类型特化

流程是经典的「主模板声明在头、全特化散落在 cpp」：

```text
param_to_json.h :  template <typename OpParam> json OpParamToJson(const OpParam&);   // 仅声明
param_to_json.cpp:  template <> json OpParamToJson(const infer::LinearParam& opParam) // 每类型一份特化
                    { json j; j["hasBias"] = opParam.hasBias; ... ; return j; }
OperationBase::GetParamJson() :  return OpParamToJson(param_);   // 编译期按 Param 类型分派到特化
```

**关键约定**：特化里**只放业务字段，刻意不写 `rsv`**。原因有二：
- `rsv` 是预留区，其内容无业务含义，序列化它会产生无意义的噪声。
- 测试反序列化时若把 JSON 里的 `rsv` 写回结构，可能破坏「`rsv` 必须全 0」的不变量（虽然反序列化代码对 `rsv` 做了单独的容错处理，见 4.4.3）。

因此 JSON 是 Param 的「业务视图」，而 `rsv` 是「二进制布局视图」的一部分——两者分离。

注意 `OpParamToJson` 是**单向（C++ → JSON）**。反向（JSON → C++）没有统一模板，而是在测试工厂里逐字段手写（见 4.4）。这是因为反序列化需要处理「字段缺失时取默认值」「枚举类型转换」等逻辑，不便用模板统一。

#### 4.3.3 源码精读

**(1) 模板声明**。只有一行主模板，没有任何默认实现——意思是「未特化的类型调用它将链接失败」，强制每个 Param 都得显式特化：

[src/atb/utils/param_to_json.h:13-15](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_to_json.h#L13-L15)：`template <typename OpParam> nlohmann::json OpParamToJson(const OpParam &opParam);`

**(2) 最朴素的特化**。`ActivationParam` 只有两个业务字段，是最简洁的样板：

[src/atb/utils/param_to_json.cpp:18-26](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_to_json.cpp#L18-L26)：把 `activationType/scale/dim` 三个业务字段塞进 json，**没有 `rsv`**。

**(3) 对照 Param 定义看「业务字段 vs rsv」**。以 `LinearParam` 为例，先看它的完整字段表：

[include/atb/infer_op_params.h:1391-1471](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1391-L1471)：`LinearParam` 含 `transposeA/transposeB/hasBias/outDataType/enAccum/matmulType/quantMode` 七个业务字段，末尾 `uint8_t rsv[21] = {0};`（L1470）。

再看它的 JSON 特化，恰好只覆盖这七个业务字段：

[src/atb/utils/param_to_json.cpp:291-302](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_to_json.cpp#L291-L302)：`OpParamToJson(const infer::LinearParam&)` 输出七个键，无 `rsv`。与上面的 Param 定义一一对照，可清楚看到「JSON = 业务字段投影」。

**(4) 嵌套结构的处理**。复杂 Param（如带子结构的 `LayerNormParam`）会先构造子 json 对象再挂到父对象上，见同文件 L240-L260（`normParam`/`preNormParam`/`postNormParam` 分层）。规律：**字段一一映射成 json 键，嵌套 struct 映射成嵌套 json 对象**。

**(5) 它在哪里被调用**。高层 Operation 通过 `OperationBase` 的钩子暴露：

[docs/starting_from_a_simple_operator.md:720-721](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L720-L721)：`GetParamJson() const { return OpParamToJson(param_); }`——一行转发，编译期按 `param_` 的类型匹配到对应特化。

**(6) `rsv` 闸门的另一面**。`rsv` 不进 JSON，但它进「工厂校验」。`OP_PARAM_RSV_CHECK` 宏在 `CreateOperation` 入口逐字节扫描：

[src/atb/operation/op_param_funcs.h:72-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L72-L80)：`OP_PARAM_RSV_CHECK(opParam)`——任何字节非 0 立即 `return ERROR_INVALID_PARAM`。这就是「JSON 不管 rsv，但创建算子时 rsv 必须干净」的完整闭环。

#### 4.3.4 代码实践

> **实践目标**：为一个虚构的新算子写一份合规的 `param_to_json` 模板特化。

设虚构算子 `ScaleAdd`：`z = a*x + y`，其 Param 为（示例代码，仓库中不存在）：

```c++
// 示例代码：include/atb/infer_op_params.h 中新增
struct ScaleAddParam {
    float scale = 1.0f;          // 业务字段 a
    bool  hasBias = false;       // 业务字段
    uint8_t rsv[16] = {0};       // 预留闸门
};
```

请在 `src/atb/utils/param_to_json.cpp` 仿照 L291-L302 写出特化（示例代码）：

```c++
// 示例代码：src/atb/utils/param_to_json.cpp 中新增
template <> nlohmann::json OpParamToJson(const infer::ScaleAddParam &opParam)
{
    nlohmann::json paramsJson;
    paramsJson["scale"]    = opParam.scale;     // 仅业务字段
    paramsJson["hasBias"]  = opParam.hasBias;
    // 注意：不要写 paramsJson["rsv"] = ...
    return paramsJson;
}
```

1. **操作步骤**：对照 4.3.3 的 (2)(3)，确认你的特化里 ① 字段名与 Param 成员名一致；② 只含业务字段；③ 返回 `nlohmann::json`。
2. **需要观察的现象**：若你漏写了某个业务字段（比如忘了 `hasBias`），编译不会报错，但图 dump 与测试反序列化时该字段会丢失、回退到默认值，造成隐蔽的行为偏差。
3. **预期结果**：理解「JSON 特化是 Param 业务字段的 1:1 投影，漏写即静默丢失」。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `OpParamToJson` 写成自动遍历结构体字段（反射），而要每个 Param 手写一份特化？

> **答案**：C++ 原生不支持结构体反射，无法在运行期枚举 `struct` 的成员名与类型。手写特化是显式、可控的做法，代价是每新增一个 Param 要多写一段机械代码，但好处是字段映射完全可见、可裁剪（如故意排除 `rsv`）、可重命名（JSON 键名可与 C++ 成员名不同）。ATB 选择了显式优于自动。

**练习 2**：`OpParamToJson` 是「单向」的。如果你只写了正向特化、却没在测试工厂里写反向反序列化，会怎样？

> **答案**：正向 JSON 化仍可用于图上报/dump，但该算子**无法被 JSON 驱动的测试框架创建**（4.4 的 `g_funcMap` 找不到它的反序列化函数），也就无法纳入回归测试。两件事是独立的，必须分别交付——这正是 4.1.2 把 `param_to_json.cpp` 和 `operation_funcs.cpp` 列成两个独立交付件的原因。

---

### 4.4 测试反序列化与算子清单（operation_funcs + op_list.yaml）

#### 4.4.1 概念说明

本模块讲两件最后落地的交付件：

1. **测试反序列化（`operation_funcs.cpp`）**：ATB 测试框架用 JSON 文本描述一个测试用例（`{"opName": "LinearOperation", "param": {...}, ...}`）。框架需要一个「字符串 opName + JSON → 真实 `Operation*`」的工厂，这就是 `g_funcMap` 与每个 `XxxOperationCreate` 反序列化函数的职责。它和 4.3 的 JSON 化共同构成 Param 的「JSON 往返」。

2. **算子清单 `op_list.yaml`**：这是 **Kernel 构建期**的交付件，决定「某个算子的某个 Kernel 在哪些芯片（ascend910b/ascend910a/ascend310p…）上编译」。它由 CMake 在首次构建时自动扫描生成，官方教程强调「这一步非常重要，否则新算子的实现与接口不会在后续构建中真正完成」。

#### 4.4.2 核心流程

**测试反序列化流程**：

```text
JSON 文本用例 {"opName":..., "param":{...}}
        │  nlohmann::json::parse
        ▼
g_funcMap.find(opName)  ──找不到──> ERROR_INVALID_PARAM（"not support opName"）
        │ 找到
        ▼
XxxOperationCreate(paramJson, &op):
    构造默认 Param;
    对每个业务字段: if (paramJson.contains(key)) param.field = paramJson[key].get<T>();
    对 rsv:           if (paramJson.contains("rsv")) 逐字节回填（容错）
    return CreateOperation(param, op);   ← 这里再过一次 rsv 闸门 + ini 校验
```

关键设计：**每个字段都用 `if (contains)` 包裹**，意味着 JSON 里缺省的字段会保留 Param 的默认值——这正是测试用例可以只写「关心的字段」的原因。

**op_list.yaml 流程**：

```text
CMake 配置阶段 (src/kernels/kernels/CMakeLists.txt)
   │  若 op_list.yaml 不存在
   ▼
python3 op_list_utils.py -s <源目录> -d <输出目录>   扫描所有算子 CMakeLists → 生成 op_list.yaml
   │
   ▼
op_list_utils.build_cmake_options(yaml, op_build.cmake)   把 yaml 翻译成 CMake 构建选项
   │
   ▼
include(op_build.cmake)   按清单把各算子在指定芯片上编译进 libasdops
```

#### 4.4.3 源码精读

**(1) 反序列化函数样板**。`LinearOperationCreate` 是最完整的范本，展示了「字段级条件回填 + 枚举类型转换 + rsv 容错 + 调用工厂」四件套：

[tests/framework/c++/atb_torch/operation/operation_funcs.cpp:475-509](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c%2B%2B/atb_torch/operation/operation_funcs.cpp#L475-L509)：逐字段 `if (paramJson.contains(...))` 回填，枚举用 `LinearParam::MatmulType(json.get<int>())` 显式转型，最后 `return CreateOperation(param, op)`。

注意 L503-L507 对 `rsv` 的处理：即便 JSON 里带了 `rsv`，也单独循环回填，随后 `CreateOperation` 内部的 `OP_PARAM_RSV_CHECK` 仍会再校验一次——双重保险。

**(2) 工厂表 `g_funcMap`**。所有反序列化函数被登记进一张 `opName → 函数指针` 的 map：

[tests/framework/c++/atb_torch/operation/operation_funcs.cpp:2528-2554](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c%2B%2B/atb_torch/operation/operation_funcs.cpp#L2528-L2554)：`g_funcMap` 定义，`{"LinearOperation", &LinearOperationCreate}` 是典型登记项。**键名必须与高层 Operation 名、ini 段名体系一致**——这是「注册名一致铁律」在测试侧的落点。

**(3) JSON 驱动测试入口**。框架统一入口把字符串 opName 与 JSON 字符串 param 路由到对应函数：

[tests/framework/c++/atb_torch/operation/operation_funcs.cpp:2619-2635](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c%2B%2B/atb_torch/operation/operation_funcs.cpp#L2619-L2635)：`CreateOperation(opName, param-string, operation)`——先 `json::parse`，再 `g_funcMap.find(opName)`，命中后 `it->second(paramJson, operation)`，并用 `try/catch` 捕获 JSON 解析异常。**没登记进 `g_funcMap` 的算子，框架直接报 `not support opName` 拒绝运行。**

**(4) 官方教程对这一步的要求**。教程明确：除了写反序列化函数，还要登记进 `g_funcMap`，两者缺一不可：

[docs/starting_from_a_simple_operator.md:98-122](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L98-L122)：`AddcustomOperationCreate` 函数体与 `{"AddcustomOperation", &AddcustomOperationCreate}` 登记项。

**(5) op_list.yaml 的自动生成**。在 Kernel 层 CMake 里，首次构建会扫描算子源目录生成清单，再翻译成构建选项：

[src/kernels/kernels/CMakeLists.txt:19-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/CMakeLists.txt#L19-L39)：`if(NOT EXISTS .../op_list.yaml)` 时调用 `op_list_utils.py` 生成 yaml，再用 `build_cmake_options` 生成 `op_build.cmake` 并 `include`。教程里手写的清单片段：

```yaml
AddcustomOperation:
    AddcustomKernel:
        ascend910b: true
```

含义：`AddcustomOperation` 这个算子的 `AddcustomKernel` 在 `ascend910b` 芯片上启用编译。`true`/`false` 控制是否为该芯片产出 Kernel 二进制。**漏登 = 该芯片上该 Kernel 不被编译 = 运行时取不到 Kernel。**

**(6) ops_customize 的平行轻量版**。`ops_customize`（u6-l5 详讲）在不重编 ATB 的前提下开发自定义算子，它有自己一套平行但更小的配置：

- [ops_customize/include/customize_op_params.h:39-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/include/customize_op_params.h#L39-L44)：`customize::BlockCopyParam`，骨架与主仓完全一致（业务字段 + `rsv[16]`），只是放在 `customize` 命名空间。
- [ops_customize/customize_ops_configs/customize_ops_info.ini:1-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/customize_ops_configs/customize_ops_info.ini#L1-L22)：`[CustomizeBlockCopyOperation]` 段，语法与主仓 `atb_ops_info.ini` 完全相同。

这印证了：**配置体系是一套统一的「契约模板」，主仓与 customize 共用同一套 ini/Param/rsv 规则，只是文件位置与命名空间不同。**

#### 4.4.4 代码实践

> **实践目标**：为虚构的 `ScaleAdd` 算子补齐「测试反序列化 + g_funcMap 登记」两步，体会 JSON 往返闭环。

承接 4.3.4 的 `ScaleAddParam`，请在 `tests/framework/c++/atb_torch/operation/operation_funcs.cpp` 仿照 L475-L509 写出（示例代码）：

```c++
// 示例代码
static atb::Status ScaleAddOperationCreate(const nlohmann::json &paramJson, atb::Operation **op)
{
    atb::infer::ScaleAddParam param;                       // 先取默认值
    if (paramJson.contains("scale")) {
        param.scale = paramJson["scale"].get<float>();     // 条件回填
    }
    if (paramJson.contains("hasBias")) {
        param.hasBias = paramJson["hasBias"].get<bool>();
    }
    if (paramJson.contains("rsv")) {                       // rsv 容错回填
        for (size_t i = 0; i < paramJson["rsv"].size(); i++) {
            param.rsv[i] = paramJson["rsv"].at(i).get<int8_t>();
        }
    }
    return CreateOperation(param, op);                     // 再过 rsv 闸门 + ini 校验
}
```

并在 `g_funcMap`（L2528 起）里加一行（示例代码）：

```c++
{"ScaleAddOperation", &ScaleAddOperationCreate},
```

1. **操作步骤**：写出函数 + 登记表项，确认键名 `ScaleAddOperation` 与你的高层 Operation 名、ini 段名一致。
2. **需要观察的现象**：若只写函数、忘了登记 `g_funcMap`，测试框架运行时会打印 `not support opName: ScaleAddOperation`（L2625）并返回 `ERROR_INVALID_PARAM`——算子「存在却测不到」。
3. **预期结果**：理解「测试可达 = 反序列化函数 + g_funcMap 登记两件套缺一不可」，且 JSON 用例里缺省的字段会自动取 Param 默认值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `XxxOperationCreate` 里每个字段都要 `if (paramJson.contains(key))` 包起来，而不是直接 `param.field = paramJson[key]`？

> **答案**：为了让 JSON 用例可以**只写关心的字段**，未写的字段保留 Param 结构里声明的默认值（如 `hasBias = true`）。直接取值在键缺失时会抛 `nlohmann::json` 异常，虽然外层 `CreateOperation`（L2628-L2633）有 `try/catch` 兜底，但那样会整条用例失败而非优雅回退默认值。`contains` 模式是 ATB 测试用例能保持简洁的根本。

**练习 2**：`op_list.yaml` 里把某算子的 `ascend910b` 从 `true` 改成 `false`，重新编译后运行该算子会发生什么？

> **答案**：构建期不再为 `ascend910b` 编译该 Kernel 二进制（`add_kernel` 不产出该芯片产物）。运行时该算子的 `GetBestKernel`/`GetKernelByName` 取不到对应 Kernel，返回 `nullptr`，算子在 910B 上不可用。注意 `op_list.yaml` 是首次构建自动生成、之后手工维护的，所以「新增算子后必须手工补登清单」是教程反复强调的关键步骤。

---

## 5. 综合实践

把本讲三块配置串成一个完整的「新算子交付」练习。沿用虚构的 `ScaleAdd`（`z = scale*x + y`，2 输入 1 输出，支持 float16/bf16 两种 dtype），请按清单交付以下**全部配置件**（均为示例代码，仓库中不存在）：

**(1) Param 定义**（`include/atb/infer_op_params.h`）

```c++
struct ScaleAddParam {
    float scale = 1.0f;
    uint8_t rsv[16] = {0};
};
```

**(2) ini 规格**（`ops_configs/atb_ops_info.ini`）

```ini
[ScaleAddOperation]
input0.name=x
input0.dtype=float16,bf16
input0.format=nd,nd
input1.name=y
input1.dtype=float16,bf16
input1.format=nd,nd
output0.name=z
output0.dtype=float16,bf16
output0.format=nd,nd
```

**(3) param_to_json 特化**（`src/atb/utils/param_to_json.cpp`）

```c++
template <> nlohmann::json OpParamToJson(const infer::ScaleAddParam &opParam)
{
    nlohmann::json paramsJson;
    paramsJson["scale"] = opParam.scale;
    return paramsJson;   // 不含 rsv
}
```

**(4) 测试反序列化 + 登记**（`tests/.../operation_funcs.cpp`）

```c++
static atb::Status ScaleAddOperationCreate(const nlohmann::json &paramJson, atb::Operation **op)
{
    atb::infer::ScaleAddParam param;
    if (paramJson.contains("scale")) { param.scale = paramJson["scale"].get<float>(); }
    if (paramJson.contains("rsv"))   { /* 逐字节容错回填，略 */ }
    return CreateOperation(param, op);
}
// g_funcMap 中追加： {"ScaleAddOperation", &ScaleAddOperationCreate},
```

**(5) op_list.yaml 登记**（`src/kernels/configs/kernels/op_list.yaml`，编译后生成）

```yaml
ScaleAddOperation:
    ScaleAddKernel:
        ascend910b: true
```

**自检与思考题**：

1. 把上述五项与 4.1.2 的清单逐条对照，确认没有遗漏（提示：还差 `params.h` 聚合与 MKI 层 Param 头，属于代码交付件）。
2. 若你的 `ScaleAdd` 后续要扩展支持 int8 量化输入，需要同步改动上述哪几项？（预期：Param 加字段、ini 加 int8 组合、param_to_json 加键、反序列化加 `contains` 分支——四处联动。）
3. **待本地验证**：在真实环境里完成 u6-l2/u6-l3 的代码交付件后，补齐本讲五项配置，重新 `bash scripts/build.sh --clean-first`，然后用 `example/op_demo` 写一个最小调用，验证 `Setup/Execute` 走通且 `CheckIniMatch` 不报错。

## 6. 本讲小结

- 一个新算子的交付件分**代码**（u6-l2/u6-l3）与**配置**（本讲）两大类；配置件让算子「能被校验、能被序列化、能被测试、能被构建」。
- **ini（`atb_ops_info.ini`）**用「逗号并列 = 支持组合」声明每个算子合法的 dtype/format 组合，运行时由 `CheckIniMatch` 按 `supportIdx` 枚举校验，失败返回 `ERROR_INVALID_TENSOR_INI_MATCH`；段名（IR key）简单算子用静态名、复杂算子（如 Linear）由 Param 字段动态拼接。
- **Param 的 JSON 化**靠函数模板 `OpParamToJson` 的逐类型全特化实现，只投影业务字段、**刻意排除 `rsv`**；`rsv` 走另一条路——由 `OP_PARAM_RSV_CHECK` 在 `CreateOperation` 入口逐字节校验，构成版本兼容闸门。
- **测试反序列化**在 `operation_funcs.cpp` 里手写 `XxxOperationCreate`（字段级 `contains` 回填 + 枚举转型 + rsv 容错）并登记进 `g_funcMap`，键名须与算子名/ini 一致；JSON 用例缺省字段自动取 Param 默认值。
- **`op_list.yaml`** 是构建期交付件，决定「哪个 Kernel 在哪些芯片编译」，首次构建自动生成、之后手工维护，漏登会导致运行时取不到 Kernel。
- 注册名一致铁律在本讲新增两个落点：**ini 段名（IR key）**与**`g_funcMap` 键名**；主仓与 `ops_customize` 共用同一套 ini/Param/rsv 契约模板。

## 7. 下一步学习建议

- **u6-l5（ops_customize 独立编译）**：本讲末尾已点出 `ops_customize` 的平行配置体系，下一讲将完整讲解如何「不重编 ATB」就交付一个自定义算子，是把本讲配置件落到独立编译流程的实战。
- **u7-l3（测试框架与算子测试）**：本讲的 `g_funcMap` 只是测试入口，下一阶段会讲完整的 JSON 驱动测试用例写法、精度/性能测试的组织方式。
- **延伸阅读源码**：建议打开 `ops_configs/atb_ops_info.ini` 通读几个你熟悉的算子（Linear/SelfAttention/AllReduce）的规格段，并对照它们在 `linear_operation.cpp` 里动态拼 IR key 的代码，巩固「Param 字段 → ini 段名 → 校验组合」的三角关系。
```
