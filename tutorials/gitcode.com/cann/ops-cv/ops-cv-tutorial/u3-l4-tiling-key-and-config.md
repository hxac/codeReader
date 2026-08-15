# u3-l4 TilingKey 多策略与多架构适配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 TilingKey 解决什么问题：为什么一份算子实现里要区分出多个"策略分支"，以及 Host 侧 `SetTilingKey` 与 Device 侧 `TILING_KEY_IS` 是如何一一对应起来的。
2. 掌握两种声明 TilingKey 的工程写法：add_example 的"模板参数声明宏"（新式、编译期展开）和 resize_bilinear_v2 的"手写十进制常量"（老式、自由编码）。
3. 理解多架构适配的组织方式：`op_host/arch35`、`op_kernel/arch35` 子目录按芯片架构隔离实现，def 文件用 `AddConfig("ascend950", ...)` 把算子绑定到具体芯片。
4. 读懂 `op_host/config/ascend950` 下的 `binary.json` 与 `simplified_key.ini` 两个配置文件分别控制什么。

本讲承接 u3-l3（Tiling 机制）。u3-l3 回答了"tiling 怎么算"，本讲回答"tiling 算完之后，怎么把'我选了哪种策略'这句话告诉 kernel"。

## 2. 前置知识

- **TilingKey 是什么**：一个 `uint64_t` 整数，由 Host 侧 TilingFunc 在运行期写入（`context->SetTilingKey(...)`），随 TilingData 一起传给编译/执行框架。框架用它来选择**编译产物**（哪个 kernel 二进制）与**执行分支**。你可以把它理解成"策略编号"。
- **为什么需要多个策略**：同一个算子在不同 shape / dtype / format 下，最优实现完全不同。例如 resize 时"输入输出尺寸相同"根本不需要插值，直接搬运（copy）就是最快实现；"整数倍缩小"则可以退化为取点。把每种场景写成独立的 kernel 类，再用 TilingKey 路由，比写一个"万能但慢"的实现划算得多。
- **Host 侧 / Device 侧约定**（u3-l1 已建立）：Host 侧 `SetTilingKey` 写入的值，必须和 kernel 侧判断宏读到的值是**同一个数**。两边的常量定义各自独立（一侧在 `op_host`，一侧在 `op_kernel`），靠人工保持一致——这是跨侧契约，改一边不改另一边就是事故。
- **arch35 / ascend950**：本仓库面向的新一代芯片架构代号。`arch35` 目录名对应 DAV_3510 一类新架构（u2-l2 提过 `IsRegBase()` 判断的 `DAV_3510`），`ascend950` 是 def 文件里的芯片型号字符串。
- **opc 编译**：离线把 Ascend C 源码编译成 kernel 二进制的工具，`binary.json` / `simplified_key.ini` 都是在描述"为哪些 dtype/format 组合各编一个二进制"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/add_example/op_kernel/add_example_tiling_key.h` | 新式写法：用声明宏描述 TilingKey 的模板参数空间 |
| `examples/add_example/op_host/add_example_tiling.cpp` | Host 侧按 dtype 调 `GET_TPL_TILING_KEY` 并 `SetTilingKey` |
| `examples/add_example/op_kernel/add_example.cpp` | kernel 侧按模板参数 `schMode` 分发到 float/int32 实现 |
| `common/inc/op_host/tiling_key.h` | 公共层：十进制位组装 TilingKey 的 `GET_TILINGKEY` 工具 |
| `common/inc/op_host/tiling_templates_registry.h` | 公共层：按 soc_version + priority 注册多个 tiling 类的注册表 |
| `image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp` | 老式写法的主力案例：shape 匹配选策略、手写 key 常量 |
| `image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp` | kernel 侧 `TILING_KEY_IS` 分发到 9 个实现类 |
| `image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json` | 描述各 dtype/format 组合对应的二进制产物 |
| `image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_simplified_key.ini` | 控制 opc `--simplified_key_mode` 选项的取值 |
| `image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp` | def 文件：`AddConfig("ascend950", ...)` 把算子绑定到芯片 |

## 4. 核心概念与源码讲解

### 4.1 模块一：TilingKey 的最小闭环（add_example 的新式写法）

#### 4.1.1 概念说明

TilingKey 最简单的用法是"按数据类型选实现"。add_example 支持 float 和 int32 两种输入，kernel 里对应两个模板实例。这个"类型 → 编号"的映射，新式写法不用手写魔法数字，而是用一组声明宏描述出来，让编译器自动生成编号与解析代码。

这套宏来自 CANN 的 `ascendc/host_api/tiling/template_argument.h`，好处是：编号生成、合法值枚举、选择逻辑都由宏统一展开，Host/Kernel 两侧共享同一份头文件，天然不会出现"两边数字对不上"的事故。

#### 4.1.2 核心流程

```
Host 侧（op_host/add_example_tiling.cpp）
  读取输入 dataType
  ├── DT_FLOAT  → tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0)  // mode 0
  └── DT_INT32  → tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_1)  // mode 1
  context->SetTilingKey(tilingKey)

框架（编译期已按模板参数展开出两个 kernel 变体）
  按 tilingKey 选中 schMode=0 或 schMode=1 的实例

Device 侧（op_kernel/add_example.cpp）
  template <uint32_t schMode> add_example(...)
  if constexpr (schMode == 0) → AddExample<float>
  if constexpr (schMode == 1) → AddExample<int32_t>
```

注意 kernel 侧用的是 `if constexpr`（编译期分支），不是运行期 `if`——每个 schMode 对应一个独立编译出来的完整 kernel，互不包含对方的代码。

#### 4.1.3 源码精读

先看 TilingKey 的声明文件，它定义了一个名为 `schMode` 的模板参数，取值只有 0 和 1 两种：

[examples/add_example/op_kernel/add_example_tiling_key.h:L21-L28](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28)

- `ELEMENTWISE_TPL_SCH_MODE_0/1`：两个策略模式常量（0=浮点，1=int32）。
- `ASCENDC_TPL_ARGS_DECL(...)`：向框架声明"本算子的 kernel 模板有一个 uint 参数 schMode，合法取值是 {0, 1}"。编译体系据此为每个取值各生成/编译一份 kernel 变体。
- `ASCENDC_TPL_SEL(...)`：配套的"选择器"声明，供 Host 侧生成 `GET_TPL_TILING_KEY` 取号函数。

Host 侧在 TilingFunc 尾部按 dtype 取号并写入 context：

[examples/add_example/op_host/add_example_tiling.cpp:L230-L244](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_tiling.cpp#L230-L244)

这段代码在 u3-l3 已经见过 tiling 计算部分，这里聚焦最后几行：`dataType == ge::DT_FLOAT` 走 mode 0，`ge::DT_INT32` 走 mode 1，其它类型直接报错返回失败。

Device 侧的枚举与分发：

[examples/add_example/op_kernel/add_example.cpp:L24-L27](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_kernel/add_example.cpp#L24-L27) 定义了与 Host 侧 mode 对应的枚举（0=浮点、1=int32）；

[examples/add_example/op_kernel/add_example.cpp:L36-L57](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_kernel/add_example.cpp#L36-L57) 是 kernel 入口：模板参数 `schMode` 由框架根据 tilingKey 实例化，`if constexpr` 在编译期选定 `AddExample<float>` 或 `AddExample<int32_t>`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证"改 tiling key 分支会改变执行路径"。
2. **操作步骤**：
   - 打开 `examples/add_example/op_host/add_example_tiling.cpp` 第 233-244 行，把 `DT_FLOAT` 分支改为也使用 `ELEMENTWISE_TPL_SCH_MODE_1`（即 float 输入也走 int32 分支）。
   - 按 u1-l4 的流程重新编译安装算子包，运行 `test_aclnn_add_example` 样例（float 输入）。
3. **需要观察的现象**：输出数值错误（float 数据被按 int32 解释/计算），或直接计算异常。
4. **预期结果**：证明 tiling key 决定了 kernel 里实际实例化的模板分支，选错分支 = 选错实现。
5. 环境不可用时，此项**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ASCENDC_TPL_ARGS_DECL` 中的取值列表从 `{0, 1}` 改成只有 `{0}`，会发生什么？
**答案**：框架只会为 schMode=0 编译一份 kernel 变体。Host 侧再调用 `GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_1)` 生成的 key 将找不到对应二进制，int32 输入在运行/编译期报"找不到合法 kernel"类错误。

**练习 2**：为什么 kernel 侧用 `if constexpr` 而不是普通 `if`？
**答案**：`if constexpr` 在编译期裁剪分支，每个 schMode 变体的二进制里只含对应类型的代码；普通 `if` 会把 float 和 int32 两套模板实例都编进同一份二进制，体积膨胀且模板实例化可能冲突。

### 4.2 模块二：手写 TilingKey 与公共编码工具（common 层）

#### 4.2.1 概念说明

当策略维度变多（dtype × format × 切分轴 × ……），逐个宏声明会不够用，很多算子选择"手写一个整数常量当 key"。为了让大家手写的 key 不互相冲突、且结构可读，`common/inc/op_host/tiling_key.h` 提供了一套**十进制位组装**工具：把若干枚举值按"低位到高位、每位十进制"拼成一个巨大的整数。

另外，`common/inc/op_host/tiling_templates_registry.h` 提供了另一条路线：不拼数字，而是把多个 tiling 实现类按 **soc_version + priority** 注册进一张表，运行期按优先级逐个尝试。这套机制与 TilingKey 是互补关系——注册表解决"多个 tiling 类谁先试"，TilingKey 解决"一个 tiling 类内部选哪份 kernel"。

#### 4.2.2 核心流程

十进制位组装的数学含义：给定若干模板/枚举参数 \(id_0, id_1, \dots, id_{n-1}\)（每个都小于 10），生成的 key 为

\[
\text{key} = 10^{19} + \sum_{i=0}^{n-1} id_i \cdot 10^{i}
\]

即每个参数占一个十进制位，最低位是第一个参数，再整体加上 \(10^{19}\) 作为偏移（保证 key 足够大、不与手写小值冲突）。

注册表路线的流程则是：

```
编译/加载期：REGISTER_TILING_TEMPLATE_WITH_SOCVERSION(op, Class, socs, priority)
             → TilingRegistryNew 按 [soc_version][op_type] 存入工厂函数
运行期：DoTilingImpl(context)
        → 从 context 取 soc_version
        → 按 priority 从小到大逐个实例化 tiling 类并 DoTiling()
        → 返回第一个非 GRAPH_PARAM_INVALID 的结果
```

#### 4.2.3 源码精读

递归求和与取号入口：

[common/inc/op_host/tiling_key.h:L25-L31](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_key.h#L25-L31)

`RecursiveSum` 是变参递归：第一个参数落在最低位，之后每个参数乘 10 的幂次抬一位——正是上面公式 \(\sum id_i \cdot 10^i\) 的代码形态。

[common/inc/op_host/tiling_key.h:L47-L52](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_key.h#L47-L52) 在求和结果上加 `TILINGKEYOFFSET`（\(10^{19}\)），这就是 `GET_TILINGKEY`。

[common/inc/op_host/tiling_key.h:L33-L45](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_key.h#L33-L45) 的注释以 FlashAttention 为例说明了位域分配约定：从低位到高位依次是 Ub0、Ub1、Block、DataType、Format、Sparse，各占一个十进制位；其它算子可定义自己的位域。这套"每个维度占一个十进制位"的约定，使得 key 可以直接"读"出策略组合。

注册表这边，按 soc_version 组织的两级 map：

[common/inc/op_host/tiling_templates_registry.h:L92-L108](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_templates_registry.h#L92-L108)

`registry_map_` 的结构是 `map<soc_version, map<op_type, TilingCases>>`（见 [L189](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_templates_registry.h#L189)），也就是"先按芯片、再按算子名"找到候选 tiling 类集合。

按优先级逐个尝试的执行逻辑：

[common/inc/op_host/tiling_templates_registry.h:L39-L54](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_templates_registry.h#L39-L54)

`RunTilingCasesHelper` 遍历所有 case（`std::map` 按 key 升序，priority 越小越先试），只要某个类的 `DoTiling()` 返回值不是 `GRAPH_PARAM_INVALID`（即"我能处理"或"真出错"），就采纳它并返回；全部返回 `GRAPH_PARAM_INVALID`（"我不适合这个输入"）才判定失败。三态返回实现了**多候选降级**。

供算子使用的注册宏：

[common/inc/op_host/tiling_templates_registry.h:L318-L336](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_host/tiling_templates_registry.h#L318-L336)

`REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` 利用全局静态对象在库加载期完成注册（见 u3-l5 将展开的"静态注册"模式），注释明确说明 priority 越小优先级越高。

#### 4.2.4 代码实践

1. **实践目标**：用仓库里已有的调用验证十进制组装规则。
2. **操作步骤**：
   - 在仓库内全局搜索 `GET_TILINGKEY(` 与 `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION`（用 Grep 工具或编辑器搜索）。
   - 找到一处实际调用，把它的各参数代入 \(\text{key} = 10^{19} + \sum id_i \cdot 10^i\) 手算一遍。
   - 再到对应算子的 kernel 侧搜索同样的数值，确认两侧一致。
3. **需要观察的现象**：手算值与 kernel 侧 `TILING_KEY_IS(...)` 的常量完全相等。
4. **预期结果**：理解"key 是可推导的，不是随机数"。本实践为纯源码阅读型，不需要 NPU 环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个位域参数必须小于 10？
**答案**：每个参数占一个十进制位；若某参数 ≥ 10，会向高位进位，破坏其它参数的位域，解码时无法还原。

**练习 2**：`RunTilingCasesHelper` 里 `GRAPH_PARAM_INVALID` 与 `GRAPH_FAILED` 的语义区别是什么？
**答案**：`GRAPH_PARAM_INVALID` 表示"当前 tiling 类不适合这组输入，请试下一个"（可降级）；`GRAPH_FAILED` 表示"真错误"，立即返回不再尝试后续 case。

### 4.3 模块三：多策略实战——resize_bilinear_v2 的 arch35 tiling

#### 4.3.1 概念说明

resize_bilinear_v2 是本仓库多策略机制的教科书案例：同一个算子，针对不同 shape 场景准备了 **9 种实现策略**，Host 侧 tiling 函数按输入特征做"策略匹配"，命中哪个场景就设置哪个 TilingKey，kernel 侧据此进入对应的实现类。

它采用的是**手写常量**风格：key 用"万位分段"编码——1xxxx 是 C 轴并行、3xxxx 是 SIMT 通用路径、4xxxx 是各种 copy/broadcast 特化，可读性很好。

这些实现被放在 `op_host/arch35/` 与 `op_kernel/arch35/` 子目录下，是**多架构适配**的物理隔离：arch35 目录里的代码只服务新架构芯片，老架构如有不同实现，另建目录互不干扰。而"这个算子支持哪些芯片"则声明在 def 文件里。

#### 4.3.2 核心流程

Host 侧策略匹配是一个**有序 if-else 链**（顺序即优先级，特化场景在前、通用兜底在后）：

```
MatchTilingStrategyAndSetTilingKey():
  useIdx32 = 输入输出规模都在 int32 范围内?
  1. 输入输出 H/W 相等且 dtype 相同     → 40000 ALL_COPY（纯搬运）
  2. NHWC + 整数倍缩放 + C 足够宽      → 40001 POINT_COPY（取点拷贝）
  3. NCHW + 源 H=W=1（广播放大）        → 40002 NCHW_BROADCAST
  4. NHWC + 源 H=W=1                   → 40003 NHWC_BROADCAST
  5. NHWC + 放大倍数 ≥ 2 + C 足够宽    → 10000 C_PARALLEL（C 轴切分并行）
  6. NCHW 兜底：按 useIdx32 / 是否需要 HW 切分
       → 30001/30003 SIMT_NCHW(_IDX64) 或 30004/30005 SIMT_HW(_IDX64)
  7. NHWC 兜底：30000/30002 SIMT_NHWC(_IDX64)

TilingStrategy():
  switch (tilingKey_) → 调用对应的 DoTilingXxx() 计算该策略专属的切分因子

DoTiling():
  FillTilingData()               // tilingKey 也写进 TilingData 本身
  context->SetBlockDim(...)      // 核数
  context->SetTilingKey(...)     // ★ 把策略编号交给框架
```

Device 侧则是镜像的 `TILING_KEY_IS(key)` 判断链，进入对应实现类。

#### 4.3.3 源码精读

**Host 侧的 key 常量表**——"万位分段"清晰可见：

[image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L36-L46](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L36-L46)

10000 段 = C 轴并行；30000 段 = SIMT（按输出元素一维展开、每线程处理若干元素的通用路径，`_IDX64` 后缀表示索引用 uint64 防溢出）；40000 段 = copy/broadcast 特化。

**策略匹配与设 key 的核心函数**：

[image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L592-L623](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L592-L623)

注意三个细节：

- 第一行先算 `useIdx32`：输入输出总元素数和 H/W 都小于 `INT32_MAX` 才敢用 32 位索引，否则选 `_IDX64` 变体——**索引宽度也是策略维度**。
- 匹配顺序特化在前：`IsMatchAllCopy()`（H/W 都没变，直接搬运）最先试，SIMT 通用路径最后兜底。
- NCHW 兜底分支里还嵌了一个 `needHWSplit` 判断：输出 H×W 超过 `coreNum × 1024`（每核线程数）才需要 HW 二级切分，选 `SIMT_HW`。

**每个策略专属的 tiling 计算**：

[image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L625-L665](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L625-L665)

`TilingStrategy()` 先匹配 key，再 `switch` 到对应的 `DoTilingXxx()`。对比 u3-l3 的 add_example（只有一种切分方式），这里是"**一种策略一套切分算法**"：AllCopy 按输出总量均分核；PointCopy 做 N/H 两轴 `FindBest2DTiling` 再逐级扩到 W、C 轴；SIMT 只按输出元素一维均分。

**写入框架的收口**：

[image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L835-L851](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L835-L851)

`DoTiling()` 末尾三件套：`SetBlockDim(realCoreNum_)`（用多少核）、`SetTilingKey(tilingKey_)`（用哪份实现）、申报 workspace。此外 `FillTilingData()`（[L667-L697](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L667-L697)）把 `tilingKey` 也存进了 TilingData 结构本身——所以 kernel 侧既能在分发时用它（框架机制），也能在核内读 `tilingData.tilingKey` 做二级判断。

**kernel 侧的镜像分发**：

[image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L27-L38](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L27-L38) 重新定义了与 Host 侧**同名同值**的常量（还多了一个 Host 侧未使用的 `TILING_KEY_HW_CACHE 20000`，属保留值）。这就是 u3-l1 说的"同名 TilingKey 常量"跨侧约定：两边各自定义、数值必须人工保持一致。

[image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L42-L55](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L42-L55) 是 kernel 入口和 ALL_COPY 分支：`GET_TILING_DATA` 解包后，`TILING_KEY_IS(TILING_KEY_ALL_COPY)` 命中则构造 `ResizeBilinearV2AllCopy` 实例执行。

[image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L77-L87](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L77-L87) 展示了**key 之内的二级分发**：同为 C_PARALLEL key，核内再按 `tilingData.cFactor < tilingData.lenC` 选择 `ResizeBilinearV2Nc` 还是 `ResizeBilinearV2CParallel`。策略编号不必穷尽所有分支——TilingData 里的字段也能参与运行期选择。

**多架构绑定在 def 文件**：

[image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp:L63-L69](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L63-L69)

`OpAICoreConfig` 通过 `ExtendCfgInfo("opFile.value", "resize_bilinear_v2_apt")` 把算子绑定到 `resize_bilinear_v2_apt.cpp` 这份 kernel 源（这就是 u3-l1 提过的 opFile 绑定），再由 `AddConfig("ascend950", ...)` 与 `AddConfig("mc62", ...)` 声明该实现适用的芯片型号列表。

#### 4.3.4 代码实践

1. **实践目标**：验证不同 shape 确实命中不同 TilingKey。
2. **操作步骤**：
   - 阅读 [L699-L715](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L699-L715) 的 `PrintTilingData()`——它会把 tilingKey 等全部字段打进 INFO 日志。
   - 准备三组输入（NCHW float32）：① 输入输出同尺寸（如 1×3×8×8 → 1×3×8×8）；② 整数倍缩小且 half_pixel_centers=true（如 1×64×8×8 → 1×64×4×4）；③ 普通任意缩放（如 1×3×17×19 → 1×3×33×40）。
   - 按 u2-l1 的两段式接口分别调用 `aclnnResize`，开启算子 INFO 日志（环境变量方式见 docs/zh/debug/op_debug_prof.md）后过滤 `tilingData is tilingKey:` 关键字。
3. **需要观察的现象**：三组输入分别打出 tilingKey 40000（all_copy）、40001（point_copy）、30001（simt_nchw）。
4. **预期结果**：与 4.3.2 的匹配顺序推理一致。需要 ascend950 类环境，**待本地验证**；无环境时可退化为纯推理练习：对着 `MatchTilingStrategyAndSetTilingKey` 逐组代入 shape 手推 key，再与 kernel 侧分发表对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么匹配链里 ALL_COPY 要放在最前面，SIMT 放最后？
**答案**：这是"特化优先、通化兜底"的贪心顺序。ALL_COPY 条件最苛刻（H/W 都不变）但性能最好（免插值直接搬运）；SIMT 是任何 4D 输入都能处理的通用路径，性能一般，只能兜底。若顺序颠倒，特化场景永远轮不到。

**练习 2**：输入 shape 为 1×3×4096×4096（float32，约 5000 万元素），会走哪个 SIMT 变体？
**答案**：元素数超过 `UINT32_MAX`？没有（约 5×10⁷ < 4.3×10⁹），所以 `useIdx32` 为真，不会因规模选 `_IDX64`；但 NCHW 分支里若输出 H×W ≥ `coreNum × 1024` 且发生了实际缩放，`needHWSplit` 为真则选 `TILING_KEY_SIMT_HW`（30004），否则 `TILING_KEY_SIMT_NCHW`（30001）——最终取决于输出尺寸与核数，需代入具体 coreNum 判断。

**练习 3**：Host 侧 `FillTilingData` 已把 tilingKey 存进 TilingData，为什么还要 `context->SetTilingKey` 单独设一次？
**答案**：两者消费者不同。`SetTilingKey` 是给**框架**的——框架用它选择编译产物/二进制（在 kernel 启动之前就要用）；TilingData 里的 `tilingKey` 字段是给**核内代码**的——kernel 里可以做二级判断或日志。前者不能省，后者是便利字段。

### 4.4 模块四：config 目录——binary.json 与 simplified_key.ini

#### 4.4.1 概念说明

`op_host/config/<芯片型号>/` 目录存放该芯片下的**编译产物描述配置**。resize_bilinear_v2 在 `config/ascend950/` 下有两个文件：

- `binary.json`：登记"每个 dtype/format 组合对应哪个编译好的 kernel 二进制文件名"。框架运行期拿到一次算子调用后，按输入描述（dtype、format）查这张表找到 `bin_filename`。
- `simplified_key.ini`：控制 opc 工具编译二进制时 `--simplified_key_mode` 选项的取值，即"是否启用简化 key 模式来减少需要预编译的二进制数量"。

这两个文件属于**交付配置**而非源码逻辑：源码决定"支持什么"，config 描述"编出来的产物长什么样、怎么选"。

#### 4.4.2 核心流程

```
编译期：  opc 按 dtype/format 组合为每个组合编译一份 kernel 二进制
          → binary.json 登记组合 → bin_filename 映射
          → simplified_key.ini 决定 --simplified_key_mode 传值
运行期：  Host tiling 设好 TilingKey
          → 框架按 (算子名, dtype, format, tilingKey) 定位二进制
          → 加载执行对应 kernel 变体
```

#### 4.4.3 源码精读

binary.json 的结构——`op_list` 数组中一项就是一个"二进制变体"：

[image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json:L1-L30](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json#L1-L30)

第一项声明：输入 `x` 是 `bfloat16 + NCHW`，输出 `y` 也是 `bfloat16 + NCHW`，对应二进制文件 `ResizeBilinearV2_9880508e9a5b60f059e97cb6e2ca9751`（哈希名）。`"shape": [-2]` 表示任意 shape（-2 是"任意维数"通配）。整个文件共 10 项，覆盖 bfloat16/float16/float32 × NCHW/NHWC ×（同 dtype 或输出 float32）的组合——这与 def 文件里的 dtype 白名单一一对应（u3-l5 详讲）。

simplified_key.ini 的全部内容与注释：

[image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_simplified_key.ini:L1-L14](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_simplified_key.ini#L1-L14)

文件头部注释就是权威说明：该文件影响 opc 编译时 `--simplified_key_mode` 选项的取值；`default=0` 表示 ascend950 平台按 mode 0 处理（简化 key 模式关闭，走完整 tiling key 区分二进制）。注释还列出了 default 与各平台配置的优先级规则，以及"自定义 simplified key 时需显式配 None"的特殊情况。

#### 4.4.4 代码实践

1. **实践目标**：数清 binary.json 覆盖的组合，并与 def 文件白名单对账。
2. **操作步骤**：
   - 统计 `resize_bilinear_v2_binary.json` 中 `op_list` 的项数（10 项）。
   - 为每项记录 (x.dtype, x.format, y.dtype) 三元组。
   - 打开 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp) 找到 `valueDataTypeX/valueDataTypeY/resizeBilinearV2Format` 的定义（文件前半部分），核对组合是否闭合。
3. **需要观察的现象**：json 中的组合恰是 def 白名单中 x/y dtype 与 format 的笛卡尔积的有效子集，无多余无遗漏。
4. **预期结果**：建立"def 白名单（能力）→ binary.json（产物）→ TilingKey（策略）"三层映射的直觉。纯阅读型实践，无需环境。

#### 4.4.5 小练习与答案

**练习 1**：binary.json 里 `"shape": [-2]` 是什么意思？为什么 shape 可以通配？
**答案**：-2 表示任意维数/任意 shape。因为 tiling 机制在运行期才根据实际 shape 计算切分，kernel 二进制只需按 dtype/format/tilingKey 区分，与具体 shape 无关（动态 shape 算子的核心红利，def 文件里 `DynamicShapeSupportFlag(true)` 也印证了这一点）。

**练习 2**：如果把 `simplified_key.ini` 的 `default=0` 删掉会怎样？
**答案**：按文件头注释规则 3——没有 default 且没有 ascend950 平台配置时，AscendC 算子按 `simplified_key_mode=0` 处理，效果等价；但显式写 `default=0` 意图更清晰，也便于将来单独覆盖某平台。

## 5. 综合实践

**任务：为 resize_bilinear_v2 编制一份完整的 TilingKey 策略对照表。**

把下表补全并验证（格式示例已填两行）：

| TilingKey | 常量名 | Host 侧命中条件（摘要） | kernel 侧实现类 | 源文件（op_kernel/arch35/） |
| --- | --- | --- | --- | --- |
| 40000 | TILING_KEY_ALL_COPY | dtype 相同且 H/W 均不变 | ResizeBilinearV2AllCopy | resize_bilinear_v2_all_copy.h |
| 40001 | TILING_KEY_POINT_COPY | NHWC、整数倍缩小、C×dtypeSize ≥ 128 | ResizeBilinearV2PointCopy | resize_bilinear_v2_point_copy.h |
| 40002 | ？ | ？ | ？ | ？ |
| 40003 | ？ | ？ | ？ | ？ |
| 10000 | ？ | ？ | ？ | ？ |
| 30000~30005 | ？ | ？ | ？ | ？ |

步骤：

1. Host 侧条件从 [resize_bilinear_v2_tiling_arch35.cpp:L592-L623](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L592-L623) 的匹配链和各 `IsMatchXxx()` 函数（L245-L352）提取。
2. kernel 侧类名从 [resize_bilinear_v2_apt.cpp](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L42-L222) 的分发链提取；注意 10000（C_PARALLEL）一项会分裂成两个类（Nc / CParallel），要写成两行并注明核内二级判断条件 `cFactor < lenC`。
3. 检索 `op_kernel/arch35/` 目录确认每个实现类的头文件名（目录下共 10 个 .h，含基类 base.h 和 nc.h）。
4. 加分项：说明 kernel 侧 [L28](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L28) 的 `TILING_KEY_HW_CACHE 20000` 为何在 Host 侧没有对应常量（提示：保留位，当前 tiling 实现不产生该 key）。

预期成果：一张 11 行左右的对照表，能把"shape 特征 → key → 实现类"一路查到底。这张表也是日后给该算子做性能分析（u7-l3）时的路由地图。

## 6. 本讲小结

- TilingKey 是 Host tiling 阶段写入的"策略编号"，框架用它选择二进制与执行分支；Host 的 `SetTilingKey` 与 kernel 的 `TILING_KEY_IS` 是必须人工保持一致的跨侧契约。
- 两种工程写法：add_example 的声明宏（`ASCENDC_TPL_ARGS_DECL` + `GET_TPL_TILING_KEY`，编译期生成、天然防错）与 resize_bilinear_v2 的手写常量（万位分段编码，自由直观）；公共层 `tiling_key.h` 还提供十进制位组装工具 `GET_TILINGKEY`，公式为 \(\text{key} = 10^{19} + \sum_i id_i \cdot 10^i\)。
- 多策略选择的通用范式是"特化在前、通化兜底"的有序匹配链（all_copy → point_copy → broadcast → c_parallel → simt），且一种策略配套一套专属切分算法。
- 多架构适配有两层：物理上用 `op_host/arch35`、`op_kernel/arch35` 子目录隔离实现；声明上在 def 文件 `AddConfig("ascend950", ...)` 绑定芯片、`opFile.value` 绑定 kernel 源。
- `config/<芯片>/binary.json` 登记 dtype/format 组合到二进制文件的映射，`simplified_key.ini` 控制 opc 的 `--simplified_key_mode`；`tiling_templates_registry.h` 的 soc_version + priority 注册表是与 TilingKey 互补的多候选降级机制。
- 索引宽度（useIdx32）也会参与策略选择——超大规模输入自动切换 `_IDX64` 变体，这是容易忽略的隐藏分支。

## 7. 下一步学习建议

- 下一讲 **u3-l5（算子定义与编译注册：def 文件与算子信息生成）**：本讲多次引用了 def 文件的 dtype 白名单与 `AddConfig`，下一讲系统走读 `*_def.cpp` 与 `cmake/gen_ops_info.cmake`，讲清"算子信息如何被收集进编译产物"。
- 之后进入 **u4-l1（Ascend C Kernel 基础）** 与 **u4-l2（resize_bilinear_v2 的多策略 kernel 变体）**：从 kernel 侧消费 TilingKey 的视角，深入 `op_kernel/arch35/` 下各实现类的内部实现。
- 建议继续阅读的源码：`common/inc/op_host/tiling_base.h`（u3-l3 已读，可结合本讲的注册表重读三态返回）、`image/grid_sample/op_host/grid_sample_tiling.cpp`（另一个多策略匹配的实例，可自行对比其匹配链设计）。
