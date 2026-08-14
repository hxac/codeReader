# u11-l5 v2 特殊函数算子注册链路

## 1. 本讲目标

本讲以 v35（昇腾 950）平台上新增的一批特殊函数算子——`ChebyshevPolynomialT/U/V/W`、`ShiftedChebyshevPolynomialT/U/V/W`、`HermitePolynomialH/He`、`I0/I0e/I1e`、`LogNdtr`、`NextAfter`、`PolyGamma` 等——为样本，讲清一个 v2 特殊函数算子从「注册进 ASCIR」到「能被 e2e 测试验证」要走的完整链路。学完后你应该能够：

1. 独立读懂 `ascir_builtin_ops_v2.cpp` 中任意一个 `REG_ASC_IR` 注册块，说出它的输入/输出/dtype 约束/平台绑定。
2. 理解 `reg_func_v2`（如 `polygamma_v2.cpp`）如何为算子计算片上临时缓冲大小，以及 `CalcVoidTmpSizeV2` 这类默认实现的适用条件。
3. 掌握 `v2_ascir_codegen_impl.h` 中 codegen 实现类的「五件套」结构：`CalcTmpBufSize` / `GetApiCallName` / `GetApiName` / `LoadApiHeaderFiles` / `IsNodeValid`，以及 ATT 侧 `REG_ASC_IR_ATT_V2_CLASS_DEFINE` 宏与性能公式注册的配套关系。
4. 学会对照 `backend_e2e_v2` 下已有的 chebyshev/hermite 用例，为一个新特殊函数算子写出完整注册清单。

## 2. 前置知识

在学习本讲之前，你需要先理解以下概念（前序讲义已建立，这里只做一句话回顾）：

- **ASCIR 注册三元组**（u5-l1）：每个算子通过 `REG_ASC_IR` 宏注册，`Impl(v2_soc_versions, {ATT 实现创建器, codegen 实现创建器, dtype 约束})` 把算子按平台绑定到两套实现上。v2 平台（3510/5102）与 v1（2201）同名算子经 `AppendSocImpl` 合并共存。
- **reg_func 与 TmpBufDesc**（u5-l2）：codegen 实现类的 `CalcTmpBufSize()` 返回 `vector<TmpBufDesc>`，是交给下游 ATT 与 `buffer_allocate` 的「片上临时缓冲占位契约」，尺寸用 `Expression` 符号表达式表示以支持动态 shape。
- **v35 目录即平台**（u11-l1）：`autofuse/v35/` 是昇腾 950 的增量目录，源码编进共享库 `aihac_codegen`，运行期靠四张注册表（ASCIR 注册表、`ApiPerfFactory`、`AscendCApiRegistry` 等）分流。
- **regbase 设备端封装**（u5-l3 的 v2 版）：`autofuse/v35/ascendc/api_regbase/*.h` 是设备端 AscendC 实现，经 CMake 的 sed 管线包成原始字符串字面量 `*_reg_base.h`，启动期注册进 `AscendCApiRegistry`，生成期按需拼进 kernel 源码。
- **特殊函数算子**：指数学上的特殊函数（special functions）——贝塞尔函数、切比雪夫多项式、厄米特多项式、伽马函数族等。它们的特点是：逐元素（elementwise）计算、计算内部是多项式递推或级数展开、只有 `float` 一种 dtype 支持（本批算子全部 `TensorType{DT_FLOAT}`）。

**本批算子的三个「形状档位」**——这是本讲最重要的分类直觉，注册方式随档位不同而不同：

| 档位 | 阶参数 n 的形态 | 代表算子 | codegen 调用形态 |
|---|---|---|---|
| 单输入 unary | 无 n | `LogNdtr`、`I0/I0e/I1e` | `UnaryApiTmpCall` |
| n 是编译期属性 | `Attr<int64_t>("n")` | `ChebyshevPolynomialT/U/V/W`（含 Shifted） | `UnaryTemplateAttrApiTmpCall`（n 进模板参数） |
| n 是运行期输入张量 | 第二个输入 `DT_INT32` | `HermitePolynomialH/He`、`PolyGamma` | `BinaryApiTmpCall` |

为什么有这三种档位？切比雪夫多项式的递推深度 n 在框架下发时就是常量（PyTorch `torch.special` 侧 n 为 int），可以烧进模板参数让编译器展开循环；厄米特/多伽马的 n 在 ATen 语义里是张量（可逐元素不同），只能走运行期二输入。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp` | v2 builtin ops 总注册文件：所有 v2 算子的 `REG_ASC_IR` 声明 |
| `autofuse/v35/ascir/generator/v2_ascir_att_impl.h` | ATT 实现侧：`REG_ASC_IR_ATT_V2_CLASS_DEFINE` 宏批量定义 ATT 实现类 |
| `autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h` | codegen 实现侧：逐算子定义 `XxxAscIrCodegenImplV2` 类 |
| `autofuse/v35/ascir/reg_func/polygamma_v2.cpp` | PolyGamma 的 reg_func：计算临时缓冲大小 |
| `autofuse/v35/ascir/reg_func/default_reg_func_v2.h` | 全部 v2 reg_func 的声明清单 |
| `autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp` | v2 性能公式注册：本批算子统一挂 `kUnitVector` 线性模型 |
| `autofuse/v35/ascendc/api_regbase/chebyshev_polynomial_t.h` 等 | 设备端 AscendC 实现（模板递推 + VF_CALL） |
| `autofuse/v35/ascendc/api_regbase/CMakeLists.txt` | sed 管线：把 `.h` 包成 `*_reg_base.h` 原始字符串 |
| `autofuse/v35/codegen/reg_api_call/unary_template_attr_api_tmp_call.h/.cpp` | 把属性 n 变成模板实参的 api_call 生成器 |
| `autofuse/compiler/python/ascir_api.py` | Python 建图 API 侧的同名包装 |
| `autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/` | e2e 用例三件套：CMakeLists + 生成器 + kernel 测试 |
| `autofuse/tests/framework/share_graph/include/share_graph.h` | 测试共享图构造器（如 `ChebyshevPolynomialTStoreFusedGraph`） |

## 4. 核心概念与源码讲解

### 4.1 v2 builtin ops 注册

#### 4.1.1 概念说明

builtin ops 注册是算子进入融合体系的「户口登记」。`ascir_builtin_ops_v2.cpp` 里每个算子一个 `REG_ASC_IR` 块，链式调用填入：

- `.Input(name, dtype_key)` / `.Output(name, dtype_key)`：形式输入输出与 dtype 占位符。
- `.Attr<int64_t>("n")`（可选）：编译期标量属性。
- `.ComputeType(ComputeType::kComputeElewise)`：计算类型，告诉 optimize 的轴分组（u6-l3）这是 Y 轴 elementwise 算子。
- `.Impl(v2_soc_versions, {ATT 创建器, codegen 创建器, dtype 约束表})`：把实现绑定到 v2 平台（3510/5102），dtype 约束表把占位符落到具体允许的 dtype 集合。

#### 4.1.2 核心流程

一个特殊函数算子注册的决策流程：

```text
确定算子签名
  ├─ 只有 x？───────────→ 单输入 unary（LogNdtr/I0/I0e/I1e）
  ├─ n 是标量常量？─────→ .Attr<int64_t>("n")（Chebyshev 家族 8 个）
  └─ n 是张量？─────────→ .Input("n", "U") + U=DT_INT32（Hermite/PolyGamma/NextAfter 的 x1,x2）
确定 dtype 约束
  └─ 本批全部 T = DT_FLOAT（设备端 static_assert 只放行 float）
绑定实现
  └─ .Impl(v2_soc_versions, {Att 创建器, Codegen 创建器, dtype 表})
```

注意 NextAfter 虽然不是「阶参数」型算子，但它天然是双输入（x1、x2），所以与 Hermite 同属二输入档位。

#### 4.1.3 源码精读

三个档位的注册样本（均在同一个 builtin 文件中）：

**档位一：单输入 unary —— LogNdtr**。[ascir_builtin_ops_v2.cpp:267-273](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L267-L273) 声明了一个输入 x、一个输出 y、只允许 `DT_FLOAT` 的 elementwise 算子，并把它绑定到 v2 双实现。NextAfter（[L275-L282](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L275-L282)）与之同构，只是多一个 `x2` 输入。

**档位二：n 为编译期属性 —— ChebyshevPolynomialT**。[ascir_builtin_ops_v2.cpp:373-380](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L373-L380) 中出现了 `.Attr<int64_t>("n")`——这个 n 不会成为 kernel 的运行期参数，而是被 codegen 填进 `ChebyshevPolynomialTExtend<float, N>` 的模板参数。Shifted 家族（如 [L337-L344](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L337-L344)）与之完全同构。

**档位三：n 为运行期输入 —— PolyGamma 与 Hermite**。[ascir_builtin_ops_v2.cpp:284-291](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L284-L291) 把 PolyGamma 建成 `x: T + n: U → y: T`，dtype 表 `{{"T", {DT_FLOAT}}, {"U", {DT_INT32}}}` 明确阶参数必须是 int32 张量。HermitePolynomialH（[L409-L416](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L409-L416)）、HermitePolynomialHe（[L418-L425](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L418-L425)）同构。

#### 4.1.4 代码实践

1. **实践目标**：能独立读出任意一个注册块的签名语义。
2. **操作步骤**：打开 `autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp`，找到 `REG_ASC_IR(I0)`、`REG_ASC_IR(ChebyshevPolynomialW)`、`REG_ASC_IR(HermitePolynomialHe)` 三处（可在编辑器搜索 `REG_ASC_IR(I0)` 等关键字）。
3. **需要观察的现象**：三个注册块的 `.Input` 数量、是否有 `.Attr<int64_t>("n")`、dtype 约束表中占位符个数。
4. **预期结果**：I0 是单输入无属性；ChebyshevPolynomialW 是单输入带 `Attr n`；HermitePolynomialHe 是双输入（x + n:U）无属性。三者 dtype 全部只放行 `DT_FLOAT`（U 为 `DT_INT32`）。
5. 本实践为纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Chebyshev 家族的 n 用 `Attr` 而 Hermite 家族的 n 用输入张量？
**答案**：切比雪夫在框架（PyTorch `torch.special_chebyshev_*`）下发时 n 是 Python int 常量，可以烧进 C++ 模板参数让设备端 `if constexpr` 按阶数特化展开（见 4.3.3 的 `N < 0 / N == 0 / N == 1` 分支）；厄米特在 ATen 语义里 n 是张量（逐元素可不同阶），必须作为运行期二进制输入参与计算，所以注册为第二个输入并限制 `DT_INT32`。

**练习 2**：`v2_soc_versions` 在注册中起什么作用？如果同一算子名在 v1 文件里也注册了，会发生什么？
**答案**：它把实现绑定到 v2 平台（3510/5102）。v1 与 v2 同名注册经全局表的 `AppendSocImpl` 合并为「算子类型 → 多平台实现」映射而非覆盖（u5-l1 结论），运行期按 SoC 版本取出对应实现。

**练习 3**：`.ComputeType(ComputeType::kComputeElewise)` 会被下游哪个模块消费？
**答案**：optimize 的 AutoSchedule 轴分组（u6-l3）——elementwise 算子的轴归入 Y（主轴）组，参与 TilingCase 的 X×Y×R 笛卡尔积枚举。

### 4.2 reg_func_v2 注册函数

#### 4.2.1 概念说明

reg_func（u5-l2 已建立概念）是 codegen 实现类 `CalcTmpBufSize()` 的外置实现体，回答一个问题：**这个算子在 UB 上需要多大的临时缓冲？** v2 侧的声明集中在 `default_reg_func_v2.h`，实现按算子各占一个 `*_v2.cpp` 文件。

本批特殊函数算子大多**不需要专属 reg_func**——它们直接调 `CalcVoidTmpSizeV2`（空占位）或复用统一的 tmp buffer 策略。唯一的例外是 **PolyGamma**：它的设备端实现需要一块与输入张量等大的临时缓冲，因此有专属的 `polygamma_v2.cpp`。

#### 4.2.2 核心流程

`CalcPolygammaTmpSizeV2` 的计算逻辑：

```text
取节点 inputs
  ├─ input_size = GetInputSize(node_inputs)        ← 输入张量元素数的符号表达式
  ├─ input_id   = GetNonScalarAxisId(node_inputs)  ← 找非标量输入；全标量则回退 0
  └─ dtype_size = GetSizeByDataType(dtype)         ← 每元素字节数
总大小 total_size = Symbol(dtype_size) * input_size ← 仍是 Expression，支持动态 shape
返回 GetTmpBuffer(total_size)                       ← 钳到硬件上限并包成 TmpBufDesc
```

#### 4.2.3 源码精读

[polygamma_v2.cpp:16-39](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/reg_func/polygamma_v2.cpp#L16-L39) 是 PolyGamma 的 reg_func：注释明确「tmp buffer 大小等于源张量 x 的大小」，先用 `GE_ASSERT_TRUE` 保证有输入，再取非标量输入的 dtype 字节数，最后 `const Expression total_size = Symbol(data_type_size) * input_size;` 用符号乘法拼出占位公式并交给 `GetTmpBuffer`。注意它 include 的是 v1 的公共头 `reg_func/defalut_reg_func.h`（文件名本身就是仓库历史拼写，注意不是 `default`），因为 `GetInputSize` / `GetNonScalarAxisId` / `GetTmpBuffer` 这些公共工具在 v1 头里定义、v2 复用。

对照 [default_reg_func_v2.h:31-32](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/reg_func/default_reg_func_v2.h#L31-L32)：`CalcVoidTmpSizeV2` 与 `CalcPolygammaTmpSizeV2` 并列声明在同一清单里——前者是「不需要额外缓冲」的通用占位（Chebyshev/Hermite/LogNdtr/NextAfter/I0 族全部走它），后者是本批唯一的新增专属实现。这个「能默认就默认」的原则正是新增算子时 reg_func 侧改动面通常为零的原因。

#### 4.2.4 代码实践

1. **实践目标**：理解 reg_func 的「占位契约」本质与默认实现的边界。
2. **操作步骤**：在 `autofuse/v35/ascir/reg_func/` 目录下用 `grep -n "CalcVoidTmpSizeV2" *.cpp` 找到 `CalcVoidTmpSizeV2` 的定义体（在 `default_reg_func_v2.cpp` 中），阅读它返回什么；再对比 `polygamma_v2.cpp`。
3. **需要观察的现象**：两者返回的 `vector<TmpBufDesc>` 内容差异——条数、`size` 字段的表达式形态。
4. **预期结果**：`CalcVoidTmpSizeV2` 返回空/零占位；`CalcPolygammaTmpSizeV2` 返回一条 `size = dtype_size × input_size` 的待求解公式。具体返回值形态待本地验证（可在阅读 `default_reg_func_v2.cpp` 时确认空占位是空 vector 还是 0 字节条目）。
5. 若无法本地编译，本实践以源码阅读结论为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `total_size` 要用 `Expression` 而不是 `uint64_t`？
**答案**：Autofuse 支持动态 shape（u3-l1），reg_func 执行时真实 shape 未知，`dtype_size * input_size` 只能以符号表达式形式交给 ATT/求解器，运行期拿到真实 shape 后才求值（u7-l2 的约束求解正是消费这些符号）。

**练习 2**：`GetNonScalarAxisId` 返回 `UINT32_MAX` 时 PolyGamma 为什么回退 `input_id = 0`？
**答案**：全部输入都是标量时没有「非标量轴」可言，PolyGamma 的缓冲以主输入 x（第一个输入）为基准，所以回退用 `inputs[0]` 的 dtype。

**练习 3**：如果你新增的特殊函数算子设备端实现完全不需要 UB 临时缓冲，reg_func 侧要写什么？
**答案**：什么都不用写——在 codegen 实现类里把 `CalcTmpBufSize` 指到 `CalcVoidTmpSizeV2` 即可（本批 12 个算子中 11 个都是这么做的）。

### 4.3 v2 codegen 适配

#### 4.3.1 概念说明

codegen 适配是把「注册过的算子」变成「可打印的 AscendC 调用语句」。v2 侧每个算子在 `v2_ascir_codegen_impl.h` 里有一个 `XxxAscIrCodegenImplV2` 类，标准结构是五个虚函数覆写（「五件套」）：

| 方法 | 职责 |
|---|---|
| `CalcTmpBufSize(node)` | 指到 reg_func（4.2） |
| `GetApiCallName()` | 选 api_call 生成器类名（`UnaryApiTmpCall` / `BinaryApiTmpCall` / `UnaryTemplateAttrApiTmpCall`…） |
| `GetApiName()` | 设备端函数名（如 `ChebyshevPolynomialTExtend`），必须与 api_regbase 头里的函数同名 |
| `LoadApiHeaderFiles(is_dynamic)` | 要从 `AscendCApiRegistry` 取出并拼进 kernel 源码的 `*_reg_base.h` 清单 |
| `IsNodeValid(node)` | 合法性门禁：禁标量输入、校验形状一致性 |

同时 ATT 侧（`v2_ascir_att_impl.h`）与性能公式侧（`ascir_api_perf_v2.cpp`）要各加一行注册——三者合起来才是完整的「适配」。

本批算子还大量继承了一个新基类 `SimtFloatUnaryAscIrCodegenImplV2`：它让算子额外获得 **SIMT 标量表达式能力**（u11-l4 的 SIMT 路径），即当调度把该算子落进标量线程链时，能生成 `AscendC::Simt::Xxx(...)` 的逐元素表达式而不是向量 API 调用。

#### 4.3.2 核心流程

一个 ChebyshevPolynomialT 节点从调度结果到 kernel 源码的路径：

```text
ScheduledResult 中的 ChebyshevPolynomialT 节点
  → AscIrCodegenImplV2 五件套被查询
      GetApiCallName → "UnaryTemplateAttrApiTmpCall"
      GetApiName     → "ChebyshevPolynomialTExtend"
      LoadApiHeaderFiles → chebyshev_polynomial_utils_reg_base.h + chebyshev_polynomial_t_reg_base.h
  → ApiCallFactory 造出 UnaryTemplateAttrApiTmpCall
      ParseAttr: 从节点属性取 n（int64_t）
      Generate:  打印 ChebyshevPolynomialTExtend<float, N>(y[..], x[..], tmp_buf_id, calCount);
  → AscendCApiRegistry 按头文件名取出设备端定义（sed 包成的原始字符串）拼进 kernel
  → 毕昇编译器编译，模板参数 N 触发 if constexpr 特化
```

关键点：**属性 n 是在这条链的中段从「节点属性」变成「模板实参」的**——这正是 `UnaryTemplateAttrApiTmpCall` 这个专用 api_call 存在的意义。

#### 4.3.3 源码精读

**（1）SIMT 标量基类**。[v2_ascir_codegen_impl.h:123-137](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L123-L137) 定义 `SimtFloatUnaryAscIrCodegenImplV2`：`IsSimtScalarSupported` 要求「单输入单输出且 dtype 相同且是 float 家族」，`GenerateSimtScalarExpr` 统一打印 `AscendC::Simt::<ApiName>(static_cast<float>(x))`。子类只需覆写 `GetSimtScalarApiName()`。

**（2）单输入档位样本**。LogNdtr（[v2_ascir_codegen_impl.h:1066-1090](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1066-L1090)）继承该基类：空 tmp、`UnaryApiTmpCall`、设备函数 `LogNdtrExtend`、SIMT 标量名 `Normcdf`、头文件 `log_ndtr_reg_base.h`。I0/I0e/I1e（[L762-L838](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L762-L838)）同构，SIMT 标量名分别借 `j0`/`j0`/`j1`。

**（3）二输入档位样本**。PolyGamma（[v2_ascir_codegen_impl.h:1115-L1141](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1115-L1141)）直接继承 `AscIrCodegenV2`：tmp 指到 `CalcPolygammaTmpSizeV2`，调用形态 `BinaryApiTmpCall`，头文件两个（`zeta_reg_base.h` 依赖 `polygamma_reg_base.h`），还额外覆写 `IncludeApiHeaderFiles` 引入 `adv_api/math/lgamma.h`（kernel 编译期的真实头文件路径，区别于 regbase 原始字符串）。HermitePolynomialH（[L1472-L1493](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1472-L1493)）注意其 `IsNodeValid` 用了带参校验 `ValidateShapeConsistencyWithSingleOutput(node, {true, {0}})`——只要求输出与第 0 个输入（x）形状一致，n 输入允许不同形状（含标量广播）。

**（4）模板属性档位样本**。ChebyshevPolynomialT（[v2_ascir_codegen_impl.h:1380-L1401](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1380-L1401)）五件套里最特别的是 `GetApiCallName() → "UnaryTemplateAttrApiTmpCall"` 与两个头文件（utils + 本体）。

**（5）属性 → 模板参数的落地**。[unary_template_attr_api_tmp_call.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/unary_template_attr_api_tmp_call.cpp) 中 `ParseAttr` 用 `node->attr.ir_attr->GetAttrValue("n", this->n_)` 把注册时 `.Attr<int64_t>("n")` 的值取进成员；`Generate` 打印 `api_name_<dtype, n_>(y[offset], x[offset], tmp_buf_id, actual_size)`——`n_` 就这样进了尖括号。类声明在 [unary_template_attr_api_tmp_call.h:15-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/unary_template_attr_api_tmp_call.h#L15-L27)，文件末尾的 `ApiCallRegister<UnaryTemplateAttrApiTmpCall> register_...("UnaryTemplateAttrApiTmpCall")` 以字符串名把它登记进工厂——这就是 `GetApiCallName()` 返回的字符串能找到类的原因。

**（6）设备端实现**。[chebyshev_polynomial_t.h:42-57](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/chebyshev_polynomial_t.h#L42-L57) 的 `ChebyshevPolynomialTExtend<T, N>`：`static_assert(SupportType<T, float>)` 只放行 float；`if constexpr` 按 N 分三档——`N<0` 输出全 0、`N==0` 输出全 1、`N==1` 退化为 `Muls(dst, src, 1.0)`，`N≥2` 才走 [L18-L40](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/chebyshev_polynomial_t.h#L18-L40) 的 `ChebyshevPolynomialTCal` 循环递推（数学依据是切比雪夫三项递推 \( T_{k+1}(x) = 2xT_k(x) - T_{k-1}(x) \)，初值 \( T_0 = 1,\ T_1 = x \)；代码先把 `coef = 2x`、`temp1 = 1` 装进寄存器再迭代）。对照 [hermite_polynomial_h.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/hermite_polynomial_h.h)：厄米特的 n 是运行期参数（函数签名无模板 N），递推 \( H_m = 2xH_{m-1} - 2(m-1)H_{m-2} \)，且有 `HERMITE_POLYNOMIAL_H_LIMIT = 128` 的阶数上限与 `__simd_vf__` 标注——印证了两档设计的差异。

**（7）sed 管线与头文件清单**。设备端 `.h` 要进入 kernel 源码必须先登记进 [api_regbase/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L66-L90) 的 `ascendc_api_regbase_extend_src` 清单（chebyshev 10 个、hermite 2 个、log_ndtr/next_after/i0 等均已列入）；[L94-L115](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L94-L115) 的 foreach 规则对每个 `.h` 生成 `cat | sed '1i\R"===(' | sed '$a\)==="'` 的自定义命令，即包成原始字符串字面量 `*_reg_base.h`——这正是 codegen `LoadApiHeaderFiles` 所引文件名的来源（「去掉 `.h` 加 `_reg_base.h`」的命名约定两边必须严格对齐）。

**（8）ATT 与性能公式配套**。[v2_ascir_att_impl.h:20-35](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_att_impl.h#L20-L35) 的 `REG_ASC_IR_ATT_V2_CLASS_DEFINE` 宏为每个算子生成 ATT 实现类，其 `GetApiPerf()` 返回字符串 `"<算子名>V2"`（如 `"ChebyshevPolynomialTV2"`）作为性能公式查表键；[L163-L196](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_att_impl.h#L163-L196) 列出了本批全部算子的宏调用（I0/I0e/I1e 在 L167-169，LogNdtr/NextAfter/PolyGamma 在 L182-184，Chebyshev 家族 L187-194，Hermite L195-196）。查表的另一端在 [ascir_api_perf_v2.cpp:885-908](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L885-L908)：本批每个算子一行 `ApiPerfRegisterV2(kXxx, GetPerfFunc(kUnitVector), ...)`，统一复用单位向量线性成本模型——即 ATT 把它们当作「每元素固定成本」的 elementwise 算子建模（u7-l1 的 c 系数来源）。

**（9）Python 建图侧**。[ascir_api.py:1014-1037](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L1014-L1037) 的 `_shifted_chebyshev_polynomial_op` 是 8 个切比雪夫包装共用的私有工厂：驼峰转蛇形生成唯一算子名、`getattr(ascir.ops, op_type)` 反射建算子、`op_instance.attr.ir_attr.n = n` 把阶数写进属性（正是 codegen `ParseAttr` 取的那个 n）；[L967-L978](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L967-L978) 的 `PolyGamma` 则走通用二输入工厂。这一层是 u9-l1 讲过的「Python 花名册与 C++ 注册成对维护」约束在 v2 的体现。

#### 4.3.4 代码实践

1. **实践目标**：验证「属性 n → 模板参数」链路的真实性。
2. **操作步骤**：
   - 阅读 [unary_template_attr_api_tmp_call.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/unary_template_attr_api_tmp_call.cpp) 的 `Generate`，确认打印语句中 `<dtype, n_>` 的位置。
   - 对照 [chebyshev_polynomial_t.h:48-56](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/chebyshev_polynomial_t.h#L48-L56) 的三个 `if constexpr` 分支，回答：n=1 时会生成对 `ChebyshevPolynomialTCal` 的调用吗？
3. **需要观察的现象**：api_call 生成的调用形如 `ChebyshevPolynomialTExtend<float, 5>(...)`；n=1 时设备端直接走 `Muls` 短路分支。
4. **预期结果**：n=1 不会调用 `ChebyshevPolynomialTCal`（被 `if constexpr (N == 1)` 拦截，编译期即消除递推循环）。
5. 生成语句的完整形态可在跑通 4.4 的 e2e 用例后，在产物 kernel 源码中搜 `ChebyshevPolynomialTExtend` 直接看到；若暂无上板环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Hermite 的 `IsNodeValid` 用 `{true, {0}}` 而普通二输入算子（如 NextAfter）用无参版本？
**答案**：NextAfter 的 x1/x2 必须形状一致；Hermite 的第二输入 n 是阶参数张量（可以是标量或与 x 不同形状），所以只校验输出与输入 0（x）一致，跳过对 n 的形状约束。

**练习 2**：`LoadApiHeaderFiles` 返回的 `zeta_reg_base.h` 这类名字是谁消费的？如果漏列会发生什么？
**答案**：被 codegen 的 registry 消费——按文件名从 `AscendCApiRegistry` 取出原始字符串拼进 kernel 源码（u5-l3 机制）。漏列则 kernel 源码引用了未定义的设备端函数，毕昇编译期报 undefined symbol。

**练习 3**：`SimtFloatUnaryAscIrCodegenImplV2` 基类带来的能力在什么场景会被触发？
**答案**：当 ATT/调度把包含该算子的子图落到 SIMT（标量线程）路径时（u11-l4 的 IndirectLoad SIMT 标量链内联场景），需要 `GenerateSimtScalarExpr` 生成 `AscendC::Simt::Xxx(...)` 表达式；纯 Vector 调度下该能力不被使用。

### 4.4 对应 e2e 用例

#### 4.4.1 概念说明

Autofuse 的 v35 e2e（backend_e2e_v2）测试不真的上板，而是走「**编译器内核闭环**」：用 `share_graph` 构造融合图 → 跑 `Optimizer::Optimize` + `Codegen::Generate` → 断言 kernel 源码里出现期望的设备函数名 → 把产物写盘 → 单独编译 kernel 测试程序与 CPU 参考实现比对。每个算子一个目录、三件套文件。

#### 4.4.2 核心流程

```text
backend_e2e_v2/<op>_store_test/
  ├─ CMakeLists.txt                          ← 声明 CODEGEN/KERNEL_SRC/TEST_SRC 与目录注册
  ├─ <op>_store_backend_generator.cpp        ← gtest：建图→Optimize→Generate→断言+落盘
  └─ test_e2e_<op>_store_kernel.cpp          ← 设备 kernel 的执行测试
执行流：generator 先跑（产出 kernel/tiling/tiling_data 三文件）
        → kernel 测试再跑（消费这三文件）
```

#### 4.4.3 源码精读

以 ChebyshevPolynomialT 为例。[chebyshev_polynomial_t_store_backend_generator.cpp:36-79](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/chebyshev_polynomial_t_store_backend_generator.cpp#L36-L79)：`SetUp` 里换上 `RuntimeStubV2` 桩；测试体先注入 tiling 桩宏，再从共享图工厂取图（`ascir::ShareGraph::ChebyshevPolynomialTStoreFusedGraph(2)`，声明于 [share_graph.h:117-121](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/framework/share_graph/include/share_graph.h#L117-L121)，旁边就是 Shifted 版本）；随后依次 `Optimize` → `Generate`，核心断言是 `EXPECT_NE(chebyshev_result.kernel.find("ChebyshevPolynomialTExtend"), std::string::npos)`——**「kernel 源码里必须出现设备函数名」就是 e2e 对整条注册链路的验收标准**（这个字符串正是 4.3 中 `GetApiName()` 的返回值，一路传递到此）。[CMakeLists.txt:1-8](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/CMakeLists.txt#L1-L8) 用 `backend_e2e_st_test` 函数注册三件套并额外把 `v35/ascendc/api_regbase` 加进 include 路径（kernel 测试要直接 include 设备端头）。

新增用例目录会被 `scripts/test/run_autofuse_test.sh` 经 CMake/ctest 自动发现（该脚本在 [L86](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L86) 检测 `v35` 目录存在后才启用 v35 测试组），无需改调度脚本。本批算子对应的用例目录（chebyshev 8 个、hermite 2 个、i0/i0e/i1e、log_ndtr、next_after 等）均已落在 `autofuse/tests/v35/st/backend_e2e_v2/` 下。

#### 4.4.4 代码实践

1. **实践目标**：跑通一个已有特殊函数算子的 e2e，亲眼看到 kernel 产物。
2. **操作步骤**：
   - 环境准备：参照 u1-l4 完成 CANN Toolkit 安装与 `set_env.sh`。
   - 在仓库根目录执行：`sh build.sh --module=autofuse_e2e --impl=cpp --st -j 8`（`--module/--impl` 选项见 [build.sh:53-69](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/build.sh#L53-L69) 的 usage；`-j 8` 是 u1-l3 讲过的防 OOM 约定）。
   - 若只想跑单个用例，进构建目录对 `chebyshev_polynomial_t_store_test_e2e_v2` 目标执行 ctest 过滤。
3. **需要观察的现象**：ctest 输出中 chebyshev/hermite/log_ndtr/next_after 等用例通过；用例目录下生成 `chebyshev_polynomial_t_store_test_kernel.cpp` 等产物文件。
4. **预期结果**：产物 kernel 源码中能搜到 `ChebyshevPolynomialTExtend<float, N>` 形态的调用（N 为共享图里设定的阶数），验证 4.3 的属性→模板链路。
5. 本实践依赖本机 CANN 环境与完整构建，具体 ctest 目标名与产物路径待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：e2e generator 为什么要在开头注入 `REGISTER_TILING_DEFAULT`/`GET_TILING_DATA` 两个桩宏？
**答案**：generator 阶段只做编译器闭环（Optimize+Generate），不链接真实 tiling 运行时；这两个宏把 tiling 注册/取数机制替换成空操作+直接强转的桩，使 kernel 源码可以脱离运行时框架单独编译执行。

**练习 2**：如果新算子注册链路里 `GetApiName()` 拼错了设备函数名，e2e 会在哪一步失败？
**答案**：generator 的 `EXPECT_NE(kernel.find("XxxExtend"), npos)` 断言失败（若断言字符串同步拼错，则漏检到 kernel 编译一步报 undefined symbol——所以断言字符串应写设备函数本名）。

**练习 3**：`ShareGraph::ChebyshevPolynomialTStoreFusedGraph(2)` 的参数 2 是什么？
**答案**：图的维度数 `dims_size`（shape 信息中 `s0`/`s1` 两个符号维度对应关系可从 generator 的 `chebyshev_shape_info` map 看出），用于构造动态 shape 的测试图。

## 5. 综合实践

**任务：以 `polygamma_v2.cpp` 为模板，为一个假想的新特殊函数算子（如 `chebyshev_polynomial_t` 的变体 `ChebyshevPolynomialT2`，或任选一个仓库中尚不存在的特殊函数）写出在 v35 v2 链路上的完整注册清单，并逐项对照仓库中已有的 chebyshev/hermite 实现验证你的清单没有遗漏。**

按以下清单逐项写出「要改哪个文件、加什么内容」：

1. **builtin ops 注册**：在 `ascir_builtin_ops_v2.cpp` 加 `REG_ASC_IR` 块——先决定档位（无 n / `Attr n` / 二输入 n），再写 Input/Output/Attr、`kComputeElewise`、`.Impl(v2_soc_versions, ...)` 与 dtype 表。对照对象：[L373-L380](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L373-L380)。
2. **ATT 实现**：在 `v2_ascir_att_impl.h` 加一行 `REG_ASC_IR_ATT_V2_CLASS_DEFINE(Xxx)`（性能键自动为 `"XxxV2"`）。对照 [L191](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_att_impl.h#L191)。
3. **性能公式**：在 `ascir_api_perf_v2.cpp` 加一行 `ApiPerfRegisterV2(kXxx, GetPerfFunc(kUnitVector), ...)`。对照 [L897-L898](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L897-L898)。
4. **codegen 实现**：在 `v2_ascir_codegen_impl.h` 加 `XxxAscIrCodegenImplV2` 五件套，按档位选 `UnaryTemplateAttrApiTmpCall` / `BinaryApiTmpCall` / `UnaryApiTmpCall`，需要 SIMT 标量能力则继承 `SimtFloatUnaryAscIrCodegenImplV2`。对照 [L1380-L1401](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1380-L1401)。
5. **reg_func（可选）**：仅当设备端需要专属 UB 缓冲时新增 `xxx_v2.cpp` 并在 `default_reg_func_v2.h` 声明；否则 codegen 五件套直接指 `CalcVoidTmpSizeV2`。对照 [polygamma_v2.cpp:16-39](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/reg_func/polygamma_v2.cpp#L16-L39)。
6. **设备端封装**：在 `v35/ascendc/api_regbase/` 写 `xxx.h`（模板 N 走 `if constexpr` 分档 + `VF_CALL`；二输入走运行期 n 递推），并把文件名登记进 `api_regbase/CMakeLists.txt` 的清单。对照 [chebyshev_polynomial_t.h:42-57](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/chebyshev_polynomial_t.h#L42-L57) 与 [CMakeLists L66-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L66-L90)。
7. **（若引入新调用形态）api_call 生成器**：本批已备好 `UnaryTemplateAttrApiTmpCall`，一般无需新增；理解其 `ParseAttr`/`Generate` 即可。
8. **Python 建图 API**：在 `ascir_api.py` 加包装函数（属性型走 `_shifted_chebyshev_polynomial_op` 式工厂，二输入走 `_common_in_2_out_1_normal_op`）。对照 [L1014-L1037](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L1014-L1037)。
9. **e2e 用例**：新建 `backend_e2e_v2/xxx_store_test/` 三件套，并在 `share_graph` 加 `XxxStoreFusedGraph` 构造器，断言 kernel 含 `XxxExtend`。对照 [chebyshev_polynomial_t_store_test](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/chebyshev_polynomial_t_store_backend_generator.cpp#L36-L79)。

**验证方式**：把清单中每一项与仓库中 chebyshev（属性档）和 hermite（二输入档）的真实提交逐一比对，确认没有多写也没有漏写；有条件时执行 `sh build.sh --module=autofuse_e2e --impl=cpp --st -j 8` 观察用例被自动发现并执行。

## 6. 本讲小结

- 本批 v2 特殊函数算子按「阶参数 n 的形态」分三档：无 n 的 unary（LogNdtr/I0/I0e/I1e）、`Attr<int64_t>` 编译期属性（Chebyshev 家族 8 个，经 `UnaryTemplateAttrApiTmpCall` 进模板参数）、`DT_INT32` 运行期输入（Hermite/PolyGamma/NextAfter，走 `BinaryApiTmpCall`）。
- 注册链路是「一处签名 + 三处适配 + 一处设备端 + 一处 Python + 一处用例」：builtin ops 定签名，ATT 宏与 `ascir_api_perf_v2.cpp` 定建模，codegen 五件套定调用生成，api_regbase 定设备实现（sed 管线包成 `_reg_base.h`），`ascir_api.py` 定建图入口，`backend_e2e_v2` 定验收。
- reg_func 侧本批只有 PolyGamma 需要专属实现（tmp = 输入张量大小的符号表达式），其余 11 个全部复用 `CalcVoidTmpSizeV2`——「能默认就默认」是新增算子改动面小的关键。
- `SimtFloatUnaryAscIrCodegenImplV2` 基类让 unary 特殊函数同时具备 SIMT 标量表达式能力，与 u11-l4 的 IndirectLoad SIMT 路径呼应。
- e2e 的验收断言是「kernel 源码中出现 `GetApiName()` 返回的设备函数名」，一根线串起注册、调度、生成、设备端定义四层。

## 7. 下一步学习建议

- 回看 u5-l1/u5-l2 的 v1 注册机制，对比 v1 与 v2 在注册宏、reg_func 命名（`*_v2` 后缀）上的差异，巩固「同名算子多平台共存」的心智模型。
- 阅读 `autofuse/v35/ascendc/api_regbase/` 下其他家族头文件（如 `polygamma.h`、`log_ndtr.h`），体会「utils 头 + 本体头」的依赖组织方式。
- 结合 u12-l1 的测试体系讲义，学习 `run_autofuse_test.sh` 如何按 `-u/-s/-c` 选项调度 framework/ascendc_api/e2e 三类测试，并尝试为你的综合实践用例补一条 UT。
- 若关注性能建模精度，可继续追踪 `ascir_api_perf_v2.cpp` 中 `kUnitVector` 线性模型与 u11-l3 精确模型（NDDMA）的适用边界，思考特殊函数算子何时需要更精细的成本模型。
