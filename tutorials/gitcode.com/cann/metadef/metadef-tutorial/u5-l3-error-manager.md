# 错误码体系与 error_manager

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 metadef 中三套并行的状态/错误码体系（`ge::graphStatus`、`domi::Status`、`ge::Status`）各自的定位、编码方式与适用场景。
2. 手工拆解一个 `ge::Status` 错误码的 32 位位域，算出它的十进制值。
3. 解释 `StatusFactory` + `ErrorNoRegisterar` 如何在静态初始化期完成「错误码 → 描述文本」的登记，以及 `GELOGE` 打日志时如何反查这段文本。
4. 描述 `ErrorManager` 的上报-聚合-输出流程，理解 `REPORT_INNER_ERROR`、`REPORT_INPUT_ERROR` 等宏的落点。
5. 按 metadef 的惯例，为一个新接口选择正确的错误码定义方式。

## 2. 前置知识

- **状态码（Status code）**：C++ 库跨越动态库边界时通常不抛异常，而是返回一个整数状态码，调用方用 `if (ret != SUCCESS)` 判断成败。metadef 因为对 ABI 兼容要求极高（回顾 u5-l4 之前的讲解：所有对外结构都是 POD），全库采用「整数码 + 日志」的错误处理风格，从不抛异常。
- **位域编码（bit-field encoding）**：把一个 32 位整数切成几段，每段承载一个维度（谁产生的、多严重、哪个模块），好处是压缩存储且可按位拆解，代价是可读性差，需要宏来生成。
- **静态初始化期注册**：在头文件里写一个全局 `const` 对象，其构造函数在 `main` 之前（或 so 被 `dlopen` 瞬间）执行。这个手法你在 u4-l3（OpDefFactory）和 u4-l4（拷贝构造触发注册）已经见过，本讲的 `ErrorNoRegisterar` 是同一模式的又一个实例。
- **Meyers 单例**：函数内 `static` 局部变量，首次调用时构造，C++11 起由编译器保证线程安全（magic statics）。`StatusFactory::Instance()` 和 `ErrorManager::GetInstance()` 都是这种写法。
- **错误码 vs 错误消息**：错误码是机器可判定的契约（只能尾部追加、不可复用），错误消息是给人看的文本（可以随时改）。metadef 用 `StatusFactory` 把两者在运行期关联起来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `inc/external/graph/error_codes.h` | ge 图体系的 `graphStatus` 与 `GRAPH_*` 错误码常量 |
| `inc/external/register/register_error_codes.h` | domi/register 体系的 `Status` 与 `DECLARE_ERRORNO` 宏 |
| `inc/external/ge_common/api_error_codes.h` | `ge::Status` 的位域编码宏 `GE_ERRORNO`、`StatusFactory`、`ErrorNoRegisterar` |
| `pkg_inc/common/ge_common/error_codes_define.h` | 各子模块错误码的快捷定义宏（`GE_ERRORNO_COMMON` 等）与已登记的具体错误码 |
| `inc/common/util/error_manager/error_manager.h` | `ErrorManager` 上报工具的契约头（实现在仓外） |
| `pkg_inc/common/ge_common/debug/ge_log.h` | `GELOGE` 等日志宏，错误码在这里被反查成描述文本并输出 |
| `inc/common/ge_common/util.h` | `GE_CHECK_NOTNULL` 等组合了上报 + 日志 + 返回的防御宏 |
| `base/common/plugin/plugin_manager.cc` | 本仓内一个真实的「上报 E19999 + GELOGE」使用现场 |

另外两个弃用转发头只需知道存在：`inc/external/graph/ge_error_codes.h` 与 `inc/external/ge_common/ge_api_error_codes.h`，它们用 `#pragma message` 提示旧路径将在 2027-06 后移除。

## 4. 核心概念与源码讲解

### 4.1 graphStatus：ge 图体系的状态返回值

#### 4.1.1 概念说明

`ge::graphStatus` 是你在前面所有讲义里见得最多的那个返回值类型——`TypeImpl::GetSizeInBytes` 返回它、TilingFunc 返回它、`SetOutputDataType` 返回它。它是 `uint32_t` 的别名，配套两个哨兵值：`GRAPH_SUCCESS = 0` 和 `GRAPH_FAILED = 0xFFFFFFFF`。

它的设计哲学是「粗粒度成败 + 细粒度点缀」：绝大多数接口只有成功/失败两态，失败原因靠日志（`GELOGE`/`GELOGW`）传递；只有少数需要调用方做分支处理的场景（比如「图没有变化」「需要重新 PASS」）才定义独立的具名错误码。

#### 4.1.2 核心流程

一个典型调用链：

```text
调用方 → 框架接口（返回 graphStatus）
   ├─ 成功：返回 GRAPH_SUCCESS (0)
   └─ 失败：GELOGW/GELOGE 记录原因 → 返回 GRAPH_FAILED 或具名错误码
              ↑
              具名码按用途分组：参数类、内存类、算术溢出类、流程控制类
```

注意 `GRAPH_NOT_CHANGED = 1343242304` 并不是随手写的魔数——它恰好等于位域编码体系里 `GE_ERRORNO_GRAPH(NOT_CHANGED, 64, ...)` 的值（4.3 节会验证这一点），说明两套体系在数值上做了对齐。

#### 4.1.3 源码精读

类型与两个核心哨兵值定义在：

[inc/external/graph/error_codes.h:L60-L63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/error_codes.h#L60-L63)

```cpp
using graphStatus = uint32_t;
const graphStatus GRAPH_FAILED = 0xFFFFFFFF;
const graphStatus GRAPH_SUCCESS = 0;
```

上面这段把 `graphStatus` 定为无符号 32 位，并定义成败两态；`0xFFFFFFFF` 满位表示「 unspecified failure 」。

具名错误码按语义分组排列：

[inc/external/graph/error_codes.h:L65-L70](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/error_codes.h#L65-L70)

```cpp
const graphStatus GRAPH_PARAM_INVALID = 50331649;
const graphStatus GRAPH_NODE_NEED_REPASS = 50331647;
...
```

这一组是参数/流程类；随后的 [L72-L75](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/error_codes.h#L72-L75) 是内存操作类（`GRAPH_MEM_OPERATE_FAILED`、`GRAPH_NULL_PTR`、`GRAPH_MEMCPY_FAILED`、`GRAPH_MEMSET_FAILED`），[L77-L80](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/error_codes.h#L77-L80) 是算术类（加法溢出、乘法溢出、向上取整溢出）——你在 u2-l1 精读过的 `GetSizeInBytes` 溢出检查返回的正是这一组语义。

产生这些错误码的真实现场可以看 `types_impl.cc`，失败路径先打日志再返回：

[base/type/types_impl.cc:L117-L133](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L117-L133)

```cpp
GELOGW("[Check][param]GetSizeInBytes failed, element_count:%" PRId64 " less than 0.", element_count);
return GRAPH_FAILED;   // 失败语义靠日志补充，码本身只有 GRAPH_FAILED
```

#### 4.1.4 代码实践

1. **实践目标**：验证 `graphStatus` 的取值规律。
2. **操作步骤**：写一个 10 行的小程序（示例代码），包含 `inc/external` 头文件路径，打印 `GRAPH_SUCCESS`、`GRAPH_FAILED`、`GRAPH_PARAM_INVALID`、`GRAPH_NOT_CHANGED` 的十进制与十六进制值。
3. **观察现象**：`GRAPH_FAILED` 是 `0xFFFFFFFF`；`GRAPH_PARAM_INVALID` 的十六进制是 `0x03000001`——注意高字节 `03`，这正是 4.2 节 domi 体系的 sysid 编码。
4. **预期结果**：输出 `0, 4294967295, 50331649, 1343242304`。（本程序只需头文件、无需链接 so，可本地编译验证；如未配置 CANN 环境，也可直接用计算器按头文件常量验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GRAPH_SUCCESS` 必须是 0，而 `GRAPH_FAILED` 用 `0xFFFFFFFF` 而不是 1？

**答案**：0 是 C/C++ 生态的通用成功语义，使得 `if (ret)`、`!ret` 等惯用法直接可用；`0xFFFFFFFF` 满位值表示「最大程度的不确定失败」，同时避开与任何位域编码码字冲突（位域编码的合法码字最高两位不会同时为11且后续段全满，除非刻意构造）。

**练习 2**：`GRAPH_NODE_NEED_REPASS` 这种「不算出错」的状态为什么也用错误码通道返回？

**答案**：这是用整数码承载流程控制信号的惯用法——图编译引擎看到该码就知道要重跑一轮 PASS，属于「非成功但可处理」的第三态。它必须与 `GRAPH_FAILED` 区分开，否则会误判为编译失败。

### 4.2 domi::Status：register 侧的框架错误码

#### 4.2.1 概念说明

register 模块（回顾 u4-l1）服务于「模型转换适配插件」，它有自己的一套 `domi::Status`。与 `graphStatus` 的散装常量不同，domi 体系从设计之初就用宏做位域编码，并预留了「子系统 ID + 模块 ID」两级命名空间，让多个子系统在同一片 32 位空间里互不冲突地分配错误码。

#### 4.2.2 核心流程

`DECLARE_ERRORNO(sysid, modid, name, value)` 的编码公式把 32 位切成三段：

```text
 31        24 23        16 15                 0
┌───────────┬────────────┬────────────────────┐
│  sysid(8) │  modid(8)  │      value(16)     │
└───────────┴────────────┴────────────────────┘
```

\[ \text{code} = (\text{sysid} \ll 24)\ \|\ (\text{modid} \ll 16)\ \|\ \text{value} \]

例如框架子系统 `SYSID_FWK = 3` 的参数错误码：

\[ 3 \times 2^{24} + 0 \times 2^{16} + 1 = 50331649 \]

这正是 4.1 节 `GRAPH_PARAM_INVALID` 的值——两套体系在「参数非法」上数值对齐，方便上层用同一个码判断。

#### 4.2.3 源码精读

编码宏与两个子系统常量：

[inc/external/register/register_error_codes.h:L14-L22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_error_codes.h#L14-L22)

```cpp
#define SYSID_FWK 3U     // Subsystem ID
#define MODID_COMMON 0U  // Common module ID

#define DECLARE_ERRORNO(sysid, modid, name, value)                                                         \
  constexpr domi::Status name = ((static_cast<uint32_t>(0xFFU & (static_cast<uint32_t>(sysid)))) << 24U) | \
                                ((static_cast<uint32_t>(0xFFU & (static_cast<uint32_t>(modid)))) << 16U) | \
                                (static_cast<uint32_t>((0xFFFFU & (static_cast<uint32_t>(value)))));
```

这段是 domi 体系的编码公式：sysid 占 bit24–31，modid 占 bit16–23，低 16 位是模块内自编值；`0xFFU &` 掩码保证越界参数不会污染相邻段。`DECLARE_ERRORNO_COMMON` 是 sysid=3、modid=0 的便捷封装。

预登记的四个码：

[inc/external/register/register_error_codes.h:L24-L32](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_error_codes.h#L24-L32)

```cpp
namespace domi {
using Status = uint32_t;
DECLARE_ERRORNO(0U, 0U, SUCCESS, 0U);
DECLARE_ERRORNO(0xFFU, 0xFFU, FAILED, 0xFFFFFFFFU);
DECLARE_ERRORNO_COMMON(PARAM_INVALID, 1U);  // 50331649
DECLARE_ERRORNO(SYSID_FWK, 1U, SCOPE_NOT_CHANGED, 201U);
```

注意 `SUCCESS` 与 `FAILED` 刻意取全 0 /全 1，与 ge 体系语义一致；`SCOPE_NOT_CHANGED` 是 register 侧的「作用域未变化」，与 `GRAPH_NOT_CHANGED` 遥相呼应。

#### 4.2.4 代码实践

1. **实践目标**：验证 domi 位域公式的正确性。
2. **操作步骤**：手工计算 `domi::SCOPE_NOT_CHANGED`：\(3 \times 2^{24} + 1 \times 2^{16} + 201\)。
3. **观察现象**：得 \(50331648 + 65536 + 201 = 50397385\)。
4. **预期结果**：写个小程序打印 `domi::SCOPE_NOT_CHANGED` 应等于 50397385（待本地验证，纯头文件即可编译）。

#### 4.2.5 小练习与答案

**练习**：如果某子系统想用 sysid=3、modid=1 定义自己的 value=1，和 `SCOPE_NOT_CHANGED` 同 modid 不同 value，会冲突吗？

**答案**：不会。位域公式的价值就在于此：`(3<<24)|(1<<16)|1 = 50397185`，与 `(3<<24)|(1<<16)|201 = 50397385` 是不同整数。只要 (sysid, modid, value) 三元组唯一，码就唯一；反过来，已发布的码三元组绝不可改，否则破坏 ABI。

### 4.3 ge::Status 位域编码与 StatusFactory：api 侧的完整体系

#### 4.3.1 概念说明

第三套体系是 `ge::Status`（定义在 `ge_api_types.h:602`，同样是 `uint32_t` 别名）。它比 domi 体系切得更细：32 位里塞进了运行位置（host/device）、码类型（错误/异常）、级别、子系统、子模块、值六个维度。更重要的是，它第一次把「错误码数值」和「人类可读描述」用 `StatusFactory` 关联起来——登记靠静态对象，反查靠单例 map。

#### 4.3.2 核心流程

编码公式（注释里标注了各段位宽）：

```text
 31   30 29 28 27   25 24        17 16     12 11          0
┌────┬─────┬─────────┬─────────────┬─────────┬─────────────┐
│rt(2)│type(2)│level(3)│  sysid(8)   │modid(5) │  value(12)  │
└────┴─────┴─────────┴─────────────┴─────────┴─────────────┘
```

\[ \text{code} = (\text{rt} \ll 30)\ \|\ (\text{type} \ll 28)\ \|\ (\text{level} \ll 25)\ \|\ (\text{sysid} \ll 17)\ \|\ (\text{modid} \ll 12)\ \|\ \text{value} \]

完整生命周期：

```text
头文件里写 GE_ERRORNO_COMMON(PARAM_INVALID, 1, "Parameter invalid!")
   │  展开为两步
   ├─① GE_ERRORNO_DEFINE: constexpr ge::Status PARAM_INVALID = <位域计算>;
   └─② GE_ERRORNO_EXTERNAL: const ErrorNoRegisterar g_errorno_PARAM_INVALID(PARAM_INVALID, "Parameter invalid!");
          │  静态初始化期构造
          ▼
      StatusFactory::Instance()->RegisterErrorNo(1343225857, "Parameter invalid!")
          │  存入 Meyers 单例的 err_desc_ map（先到先得，重复忽略）
          ▼
运行期 GELOGE(PARAM_INVALID, "...") 打日志时
      GE_GET_ERRORNO_STR(PARAM_INVALID) → GetErrDesc(1343225857) → "Parameter invalid!"
      dlog_error 输出 "ErrorNo: 55(Parameter invalid!)..."
```

#### 4.3.3 源码精读

编码与登记双宏：

[inc/external/ge_common/api_error_codes.h:L36-L53](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/ge_common/api_error_codes.h#L36-L53)

```cpp
#define GE_ERRORNO_DEFINE(runtime, type, level, sysid, modid, name, value)  \
  constexpr ge::Status name = (... 六段位移拼接 ...)

#define GE_ERRORNO_EXTERNAL(name, desc) const ge::ErrorNoRegisterar g_errorno_##name((name), (desc))

#define GE_ERRORNO(runtime, type, level, sysid, modid, name, value, desc) \
  GE_ERRORNO_DEFINE(runtime, type, level, sysid, modid, name, value);     \
  GE_ERRORNO_EXTERNAL(name, desc)
```

这段定义了 api 侧错误码的完整语法：`GE_ERRORNO` 一步产出「constexpr 码值常量 + 描述登记对象」；六个参数对应六个位段。注意 `GE_ERRORNO_DEFINE` 展开出的是 `constexpr`，码值在编译期就确定，登记对象则在静态初始化期执行。

登记目标 `StatusFactory`（Meyers 单例 + 先到先得的 map）：

[inc/external/ge_common/api_error_codes.h:L56-L80](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/ge_common/api_error_codes.h#L56-L80)

```cpp
static StatusFactory *Instance() {
  static StatusFactory instance;   // magic static，线程安全
  return &instance;
}
void RegisterErrorNo(const uint32_t err, const char *const desc) {
  if (desc == nullptr) { return; }
  if (err_desc_.find(err) != err_desc_.end()) { return; }  // Avoid repeated addition
  err_desc_[err] = error_desc;
}
```

这段是描述文本的登记中心：`Instance()` 用函数局部静态保证进程内唯一；`RegisterErrorNo` 对重复码直接忽略——因此多个头文件被同一程序包含时不会重复登记，但也意味着「先包含者赢得描述」。

登记的执行者 `ErrorNoRegisterar`：

[inc/external/ge_common/api_error_codes.h:L106-L115](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/ge_common/api_error_codes.h#L106-L115)

```cpp
class GE_FUNC_VISIBILITY ErrorNoRegisterar {
 public:
  ErrorNoRegisterar(const uint32_t err, const std::string &desc) noexcept {
    StatusFactory::Instance()->RegisterErrorNo(err, desc);
  }
  ...
};
```

构造即注册——与 u4-l3 的 `OpReceiver`、u3-l5 的 `REGISTER_TILING_DATA_CLASS` 完全同构的「静态对象构造期注册」手法；整个类在头文件内 inline 实现，没有对应 .cc。

具体错误码的快捷定义与已登记清单在 pkg_inc 层：

[pkg_inc/common/ge_common/error_codes_define.h:L61-L99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/common/ge_common/error_codes_define.h#L61-L99)

```cpp
enum class InnSystemIdType : std::uint8_t { SYSID_GE = 8 };
enum class InnSubModuleId : std::uint8_t { COMMON_MODULE = 0, ..., GRAPH_MODULE = 4, ... };
enum class InnErrorLevel : std::uint8_t { COMMON_LEVEL = 0b000, ..., CRITICAL_LEVEL = 0b100 };
```

这段把六个位段中的四个枚举化（子系统固定为 ge=8，子模块 12 选 1，级别 5 档），使 `GE_ERRORNO_COMMON(name, value, desc)` 这类快捷宏（[L18-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/common/ge_common/error_codes_define.h#L18-L54)）只需填 name/value/desc 三个业务参数。

已登记的具体码：

[pkg_inc/common/ge_common/error_codes_define.h:L102-L111](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/common/ge_common/error_codes_define.h#L102-L111)

```cpp
GE_ERRORNO_COMMON(PARAM_INVALID, 1, "Parameter invalid!");       // 1343225857
GE_ERRORNO_COMMON(RT_FAILED, 3, "Failed to call runtime API!");  // 1343225859
GE_ERRORNO_GRAPH(NOT_CHANGED, 64, "The node of the graph no changed.");  // 1343242304
```

验证 `PARAM_INVALID`：\(1 \times 2^{30} + 1 \times 2^{28} + 8 \times 2^{17} + 0 + 1 = 1343225857\)，与注释一致；再验证 `NOT_CHANGED`：把 modid 换成 GRAPH_MODULE=4（\(4 \times 2^{12} = 16384\)）、value 换成 64，得 \(1343225857 + 16384 + 63 = 1343242304\)——正是 4.1 节 `GRAPH_NOT_CHANGED` 的值，证实两套体系数值对齐。

反查出口：

[pkg_inc/common/ge_common/error_codes_define.h:L56-L59](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/common/ge_common/error_codes_define.h#L56-L59)

```cpp
#define GE_GET_ERRORNO_STR(value) ge::StatusFactory::Instance()->GetErrDesc(value)
#define RT_ERROR_TO_GE_STATUS(RT_ERROR) static_cast<ge::Status>(RT_ERROR)
```

`GE_GET_ERRORNO_STR` 是日志宏反查描述的唯一入口；`RT_ERROR_TO_GE_STATUS` 说明 runtime 侧错误码直接按数值强转进 ge 体系（两体系共用 `uint32_t` 底座的好处）。

#### 4.3.4 代码实践

1. **实践目标**：完整追踪 `GE_ERRORNO_COMMON(PARAM_INVALID, 1, "Parameter invalid!")` 从宏展开到日志输出的全链路。
2. **操作步骤**：
   - 读 [api_error_codes.h:L36-L53](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/ge_common/api_error_codes.h#L36-L53)，写出宏两次展开的产物。
   - 手工按位域公式计算码值（应为 1343225857，即 `0x50004001`）。
   - 读 [ge_log.h:L70-L74](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/common/ge_common/debug/ge_log.h#L70-L74) 的 `GELOGE`，找到其中的 `GE_GET_ERRORNO_STR(ERROR_CODE)` 调用。
   - （可选，需要 CANN 环境）编译一个包含 `ge_log.h` 的小程序，调用 `GELOGE(ge::PARAM_INVALID, "demo")` 并设置环境变量 `ASCEND_SLOG_PRINT_TO_STDOUT=1`。
3. **观察现象**：日志中的 `ErrorNo:` 后面出现数字与 `"Parameter invalid!"` 描述；拆解 `0x50004001` 的二进制应还原出 rt=01、type=01、level=000、sysid=00001000、modid=00000、value=000000000001。
4. **预期结果**：手工推演部分可当场完成；编译运行部分待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`GE_ERRORNO_EXTERNAL` 生成的 `g_errorno_##name` 是头文件里的全局 const 对象，被多个 .cc 包含时会不会重复定义？描述会不会被登记多次？

**答案**：不会链接冲突——C++ 中 namespace 作用域的 `const` 变量默认具有内部链接，每个编译单元各有一份。每份构造时都会调用 `RegisterErrorNo`，但 `StatusFactory::Instance()` 是 inline 函数的局部静态，全进程唯一（默认可见性下跨 so 也唯一），且登记逻辑「已存在即忽略」，所以最终 map 里只有一个条目。

**练习 2**：为什么 `SUCCESS` 也要走一遍 `GE_ERRORNO(...)` 登记？

**答案**：`GELOGE(ge::FAILED, ...)` 这类宏对任意码都会反查描述；登记 success/failed 两个端点保证反查永远有文本可回（查不到时 `GetErrDesc` 返回空串，日志里描述段会缺字）。

### 4.4 ErrorManager 与错误上报链路

#### 4.4.1 概念说明

前三套体系回答「返回什么码」，`ErrorManager` 回答「错误如何呈现给最终用户」。它是面向 ATC（模型编译工具）等场景的上报通道：错误以 6 位字符串 ID（如 `E19999`）标识，描述模板来自外部 JSON 配置文件（含错误标题、可能原因、解决方案），可按线程/会话粒度聚合后在合适的时机统一输出。

一个关键事实：**`ErrorManager` 只在 metadef 中有契约头，实现在仓外**（本仓 `git ls-files` 中没有任何 `error_manager.cc`，`base/` 源码也不引用它）。这和 u4-l1 讲过的 register 模块一样——metadef 提供契约，ge 等上层组件提供实现并消费。metadef 自己的代码走的是更轻的 `REPORT_INNER_ERR_MSG` 宏路径（该宏定义在 CANN 环境提供的 `base/err_msg.h` 中，同样不在本仓）。

#### 4.4.2 核心流程

```text
出错点（如 PluginManager 符号缺失）
   │ REPORT_INNER_ERR_MSG("E19999", "格式化原因...", ...)
   ▼
error_message::ReportInnerError(file, func, line, "E19999", fmt, ...)
   │ 补全 文件/函数/行号 上下文，写入当前 work_stream 对应的容器
   ▼
ErrorManager 内部：error_message_per_work_id_[work_id] 追加 ErrorItem
   ▼
调用方在 API 出口：OutputErrMessage(handle) / GetErrorMessage()
   → 聚合、套用 JSON 模板、输出，并清理容器
```

并行的另一条日志通道：

```text
GELOGE(ge::PARAM_INVALID, "同一段原因文本")
   → dlog_error(..., GE_GET_ERRORNO_STR 反查的描述, ...) → slog 日志文件/标准输出
```

两条通道各司其职：`ErrorManager` 面向用户的结构化错误（带解决方案），`GELOGE` 面向开发者的运行日志（带 tid 和函数名）。metadef 代码里的惯例是**成对出现**。

#### 4.4.3 源码精读

上报宏族：

[inc/common/util/error_manager/error_manager.h:L46-L59](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L46-L59)

```cpp
#define REPORT_INPUT_ERROR(error_code, key, value) \
  ErrorManager::GetInstance().ATCReportErrMessage(error_code, key, value)
#define REPORT_ENV_ERROR(error_code, key, value) ErrorManager::GetInstance().ATCReportErrMessage(error_code, key, value)
#define REPORT_INNER_ERROR(error_code, fmt, ...) \
  error_message::ReportInnerError(__FILE__, &__FUNCTION__[0], __LINE__, (error_code), (fmt), ##__VA_ARGS__)
#define REPORT_CALL_ERROR REPORT_INNER_ERROR
```

这段定义了四类上报入口：输入/环境类错误走 `ErrorManager::ATCReportErrMessage`（key-value 参数填充 JSON 模板占位符），内部/调用错误走自由函数 `ReportInnerError`（自动捕获文件、函数、行号）。`REPORT_CALL_ERROR` 只是 `REPORT_INNER_ERROR` 的别名。

错误数据的载体 `ErrorItem`：

[inc/common/util/error_manager/error_manager.h:L103-L117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L103-L117)

```cpp
struct ErrorItem {
  std::string error_id;        // "E19999"
  std::string error_title;
  std::string error_message;
  std::string possible_cause;
  std::string solution;
  std::map<std::string, std::string> args_map;  // 填充 error_message 的占位参数
  std::string report_time;
};
```

这个结构就是 JSON 模板在内存中的形态：五个文本字段对应错误呈现的五个段落，`args_map` 存模板占位符的实际取值。

单例的主要对外接口：

[inc/common/util/error_manager/error_manager.h:L129-L177](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L129-L177)

```cpp
int32_t Init(const std::string path);            // 从指定目录加载错误码 JSON 配置
int32_t ReportErrMessage(const std::string error_code, const std::map<...> &args_map);
int32_t OutputErrMessage(int32_t handle);        // 输出并（随后）清理
std::vector<ErrorItem> GetRawErrorMessages();    // 取原始上报序列（取出即清理）
void GenWorkStreamIdDefault();                   // 以 pid+tid 划定聚合粒度
```

这一组接口勾勒出生命周期：`Init` 加载 JSON 模板 → 上报期 `Report*` 追加 → 出口 `Output*`/`Get*` 输出并清理。`GenWorkStreamId*` 系列决定「错误按哪个粒度隔离」——对应 [L96-L101](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L96-L101) 的两种模式：推理线程粒度 / 训练 session 粒度（INTERNAL_MODE）或整个进程一份（PROCESS_MODE）；上下文经 [L273](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L273) 的 `thread_local static error_context_` 存取。

本仓内的真实使用现场（u5-l2 讲过的符号校验失败路径）：

[base/common/plugin/plugin_manager.cc:L729-L740](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L729-L740)

```cpp
const auto real_fn = reinterpret_cast<void (*)()>(mmDlsym(handle, func_name.c_str()));
if (real_fn == nullptr) {
  REPORT_INNER_ERR_MSG("E19999", "[Check][So]%s is skipped since function %s does not exist! errmsg:%s", ...);
  GELOGE(ge::PARAM_INVALID, "[Check][So]%s is skipped since function %s does not exist! errmsg:%s", ...);
  is_valid = false;
  break;
}
```

这段是「双通道成对上报」的标准样板：`REPORT_INNER_ERR_MSG` 写用户可见的结构化错误，`GELOGE(ge::PARAM_INVALID, ...)` 写开发者日志，随后返回失败并跳过该 so。防御宏把这三步固化成一行的例子见 [inc/common/ge_common/util.h:L113-L120](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/ge_common/util.h#L113-L120) 的 `GE_CHECK_NOTNULL`（上报 + 日志 + `return ge::PARAM_INVALID`）。

#### 4.4.4 代码实践

1. **实践目标**：梳理 `E19999` 从产生到输出的完整链路，并确认哪些环节在本仓、哪些在仓外。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "E19999" base/ | head -20`，统计使用点数量。
   - 挑 `plugin_manager.cc:734` 这个点，沿本讲 4.4.2 的流程图画出后续路径。
   - 执行 `git ls-files | grep -i error` 确认本仓只有 `error_manager.h` 而无其实现；再执行 `grep -rn "ErrorManager" base/` 确认 metadef 自身代码不直接引用该类。
   - 找到 `REPORT_INNER_ERR_MSG` 的定义位置：`grep -rn "define REPORT_INNER_ERR_MSG" .`——会发现它不在本仓，来自 CANN 环境的 `base/err_msg.h`（`ge_log.h:21` include 了它）。
3. **观察现象**：metadef 只依赖宏的「声明语义」，不依赖 `ErrorManager` 的具体实现，编译期通过 include 外部头解耦。
4. **预期结果**：链路图为「plugin_manager.cc 上报 → (仓外) ReportInnerError → ErrorManager 容器 → (仓外) 上层工具输出」，并能在报告中明确标注仓界。

#### 4.4.5 小练习与答案

**练习 1**：`IsValidErrorCode`（[error_manager.h:L245-L248](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/util/error_manager/error_manager.h#L245-L248)）要求错误码字符串长度恰好为 6，那 `E19999` 合法吗？这个格式是什么含义？

**答案**：合法，长度恰为 6。格式为 `E` + 5 位数字：`E1xxxx` 是参数/输入类，`E19999` 特指内部错误（inner error），`E10052` 等其余段位用于预定义的用户侧错误（`ge_log.h` 中 `GE_WARNINGLOG_AND_ERRORMSG` 就用了 E10052）。

**练习 2**：为什么 metadef 的 `base/` 代码用 `REPORT_INNER_ERR_MSG`（自由函数）而不是 `REPORT_INPUT_ERROR`（ErrorManager 成员）？

**答案**：metadef 是最底层库，不负责解析 JSON 模板、维护聚合粒度这些上层职责；自由函数 `error_message::ReportInnerError` 只需要链接期存在该符号（实现在仓外组件中），把「模板配置 + 粒度管理 + 输出」全部留给上层，符合 u4-l1 确立的「metadef 提供契约、上层提供实现」分层原则。

## 5. 综合实践

**任务：为一个假想的新接口设计完整的错误处理方案。**

假设你要在 metadef 中新增一个 `ParseSparseIndex(const char *path, int64_t length)` 接口，要求：

1. 在本地写一个设计文档（Markdown 即可），包含：
   - 返回值类型选哪套体系（提示：图编译链路上的接口用 `graphStatus`，转换适配链路用 `domi::Status`，需要参与日志反查描述的用 `ge::Status` 位域宏）并说明理由。
   - 若用 `ge::Status`：写出完整的 `GE_ERRORNO_COMMON(...)` 风格定义行（示例代码，不必提交到仓库），包含 name、value、desc，并手工算出 32 位码值、画出位段拆解图。
   - 若干个失败分支的代码骨架（示例代码）：`path == nullptr`、`length <= 0`、文件读取失败，每个分支都要体现「`REPORT_INNER_ERR_MSG` + `GELOGE` + 返回码」三件套，参考 `plugin_manager.cc:729-L740` 的样板。
2. 自查清单：新码是否只在已有 value 序列尾部追加？描述文本是否登记进了 `StatusFactory`？上报与日志是否成对出现？
3. 运行验证（待本地验证，需按 u1-l2 完成 build 环境）：把骨架代码放入一个临时 .cc，用 `bash build.sh` 编译通过，观察 `-Werror` 下格式化字符串与参数是否匹配（`__attribute__((format(printf, ...)))` 会在编译期把关）。

## 6. 本讲小结

- metadef 内三套状态体系并存：`ge::graphStatus`（图编译链路，粗粒度成败 + 具名点缀）、`domi::Status`（register/转换适配链路，sysid/modid/value 三段位域）、`ge::Status`（api 侧，六段位域 + 描述登记），三者底座都是 `uint32_t`，关键码值刻意对齐（如 50331649、1343242304）。
- `GE_ERRORNO` 宏一步产出「constexpr 码值 + `ErrorNoRegisterar` 静态登记对象」，登记在静态初始化期进入 `StatusFactory` Meyers 单例的 map，先到先得、重复忽略。
- 日志输出时 `GELOGE` 经 `GE_GET_ERRORNO_STR` 反查描述文本，与 tid、函数名一起交给 `dlog_error`——这是错误码「从产生到输出」在本仓内可见的完整闭环。
- `ErrorManager` 是面向最终用户的结构化上报通道（6 位字符串 ID + JSON 模板 + 线程/session/进程三种聚合粒度），metadef 只提供契约头，实现在仓外组件。
- metadef 自身代码的错误处理惯例是「双通道成对」：`REPORT_INNER_ERR_MSG`（用户侧结构化错误）+ `GELOGE`（开发者日志）+ 明确的返回码，防御宏 `GE_CHECK_NOTNULL`/`GE_CHK_BOOL_RET_STATUS` 把三件套固化成一行。
- 已发布的错误码数值是 ABI 契约：只能尾部追加新值，绝不可修改或复用旧值——这与 u2-l1 的枚举追加规则一脉相承。

## 7. 下一步学习建议

- 下一讲 u5-l4（ABI 兼容性设计与守护测试）将从「错误码数值不可变」引申到整个对外结构布局的 ABI 守护，建议接着学习。
- 想加深位域编码的手感，可以对照阅读 `docs/zh/api/ge_namespace/DECLARE_ERRORNO.md`，并自行验证 `api_error_codes.h` 中 `END_OF_SEQUENCE`（runtime=0b01, type=0b01, sysid=8, modid=0, value=7）的数值。
- 想看错误码在运行期被消费的更多实例，可以在 `base/` 下全局搜索 `GE_CHK_STATUS_RET` 与 `GE_CHK_BOOL_RET_STATUS`（都在 `pkg_inc/common/ge_common/debug/ge_log.h`），观察不同模块如何选择返回码与上报宏的组合。
