# u6-l1 dlsym 动态加载机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清为什么 HCCL 与 HCOMM 两个仓库必须通过 `dlsym` 动态加载解耦，而不是直接链接。
2. 跟踪 `HcommDlInit` 的完整加载流程：进程构造函数 → `HcclDlopen("libhcomm.so")` → 各域 `XxxDlInit` 逐个绑定符号。
3. 掌握 `DECL_WEAK_FUNC → DEFINE_WEAK_FUNC → INIT_SUPPORT_FLAG → XxxDlInit` 四步模式，并能为一个假想的 HCOMM 新符号独立写出接入骨架。
4. 理解本轮新增的 `HcclDlopen/HcclDlsym/HcclDlclose` 弱符号封装层解决了什么问题（可被外部替换、插桩、Mock）。

## 2. 前置知识

在进入源码前，先补齐几个 C/C++ 动态链接的基础概念：

- **动态库（.so）与 dlopen/dlsym**：Linux 下 `dlopen(path, mode)` 在运行期把一个共享库加载进进程地址空间并返回句柄；`dlsym(handle, name)` 按名字在库中查找符号（函数或变量）并返回其地址；`dlclose` 卸载。这是"运行期才决定调用谁"的机制，与编译期链接相对。
- **弱符号（weak symbol）**：用 `__attribute__((weak))` 声明的函数是一个"可以被覆盖"的默认实现。链接时如果别处提供了同名强符号，强符号胜出；否则使用弱符号版本。HCCL 用它给每一个可能不存在的 HCOMM 接口提供一个"打印错误并返回 -1"的兜底实现。
- **weak alias（弱别名）**：`weak_alias(name, alias)` 宏（glibc 同款写法）定义一个真实函数 `__name`，再给它起一个弱别名 `name`。外部可以只覆盖别名 `name`，而不必重定义 `__name`。
- **`extern "C"`**：关闭 C++ 名字修饰（name mangling），保证符号表里的名字就是源码里的名字——`dlsym` 按字符串查符号，这一步必不可少。
- **`RTLD_NOW`**：dlopen 的模式标志，表示加载时立即解析所有未定义符号，而不是等到首次调用。HCCL 选它是为了"尽早失败"——libhcomm.so 与 HCCL 不匹配时在加载瞬间暴露，而不是在算子执行中途崩溃。
- **进程构造函数（`__attribute__((constructor))`）**：共享库被加载时自动执行的函数，HCCL 借它在任何人调用任何算子之前就完成 dlsym 绑定。

回顾 u1-l1 的架构约束：HCCL 与 HCOMM 独立编译、独立版本演进，HCCL 不得在编译期依赖 HCOMM 私有头文件，跨仓调用统一走 `src/common/hcomm_dlsym/`。本讲就是这个"解耦通道"的实现课。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/hcomm_dlsym/hccl_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_dl.h) | 本轮新增：`HcclDlopen/HcclDlsym/HcclDlclose` 的 C 声明 |
| [src/common/hcomm_dlsym/hccl_dl.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_dl.cc) | 本轮新增：对系统 `dlopen/dlsym/dlclose` 的 weak_alias 封装实现 |
| [src/common/hcomm_dlsym/hcomm_dlsym.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.h) | 对外门面：`HcommDlInit`、`GetHcommVersion`、支持性查询 |
| [src/common/hcomm_dlsym/hcomm_dlsym.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc) | `HcommDlInit`：dlopen libhcomm.so 并依次驱动各域 `XxxDlInit` |
| [src/common/hcomm_dlsym/dlsym_common.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h) | 宏工具箱：`DECL_WEAK_FUNC/DEFINE_WEAK_FUNC/DECL_SUPPORT_FLAG/INIT_SUPPORT_FLAG`、版本宏与兼容桩 |
| [src/common/hcomm_dlsym/hccl_res_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h) | "资源域"符号的弱声明集合（作为样例精读） |
| [src/common/hcomm_dlsym/hccl_res_dl.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc) | "资源域"桩定义与 `HcclResDlInit` 绑定入口 |
| [src/common/compat.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/compat.cc) | 库加载构造函数：`pthread_once` 保证 `HcommDlInit` 恰好执行一次 |

## 4. 核心概念与源码讲解

### 4.1 模块一：hccl_dl.h/.cc 的 weak_alias 封装

#### 4.1.1 概念说明

本轮提交（`ec53a804`）之前，`hcomm_dlsym` 各处直接调用系统 `dlopen/dlsym/dlclose`。之后，所有动态加载统一收敛到三个新的 C 接口：`HcclDlopen/HcclDlsym/HcclDlclose`。

它们与系统函数行为完全一致，唯一的区别是：**这三个符号本身是弱符号**。任何外部程序（性能插桩工具、Mock 测试桩、安全审计器）只要在自己的编译单元里提供同名强符号定义，就能在不重编 HCCL 的前提下接管所有动态加载行为。这就是"用弱符号封装"的价值——把"加载哪个库、怎么查符号"从硬编码变成可注入点。

#### 4.1.2 核心流程

```text
HCCL 某处需要加载/查符号
        │
        ▼
调用 HcclDlopen / HcclDlsym / HcclDlclose   （弱符号）
        │
   ┌────┴─────────────────────────┐
   │ 外部提供了强符号定义？          │
   └────┬─────────────────────────┘
   是 │                    │ 否
      ▼                    ▼
外部实现接管           hccl_dl.cc 中的
（插桩/Mock）          __HcclDlopen 等
      │                    │
      └───── 都最终落到系统 dlopen/dlsym/dlclose ─────┐
                            （外部实现也可选择不落到系统调用）
```

#### 4.1.3 源码精读

先看声明——三个纯 C 接口，签名与系统 `dlopen/dlsym/dlclose` 一一对应：

[点击查看 hccl_dl.h:L18-L24](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_dl.h#L18-L24)：声明 `HcclDlopen(libName, mode)`、`HcclDlsym(handle, funcName)`、`HcclDlclose(handle)`，包在 `extern "C"` 中保证符号名不被 C++ 修饰。

再看实现——核心是 glibc 风格的 `weak_alias` 宏：

[点击查看 hccl_dl.cc:L14-L17](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_dl.cc#L14-L17)：定义 `weak_alias(name, aliasname)` 宏，展开为 `extern __typeof(name) aliasname __attribute__((weak, alias(#name)))`——给真实函数 `name` 起一个弱别名 `aliasname`。

[点击查看 hccl_dl.cc:L22-L29](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_dl.cc#L22-L29)：真实实现叫 `__HcclDlclose/__HcclDlsym/__HcclDlopen`，每个只有一行，直接透传给系统 `dlclose/dlsym/dlopen`；随后三条 `weak_alias` 把 `HcclDlopen` 等公开名定义为弱别名。

#### 4.1.4 代码实践

**实践目标**：验证 `HcclDlopen` 是弱符号、可被外部强符号覆盖。

**操作步骤**（示例代码，非项目原有文件）：

1. 写一个小测试程序 `test_override.cc`（放在仓库任何临时位置，不要提交）：

```cpp
// 示例代码：覆盖 HCCL 的弱符号 HcclDlopen
#include <cstdio>
extern "C" void* HcclDlopen(const char* libName, int mode)
{
    printf("[stub] HcclDlopen intercepted: lib=%s mode=%d\n", libName, mode);
    return nullptr; // 拦截后不再真正加载
}
int main() { return 0; }
```

2. 把它与 HCCL 的 `hccl_dl.cc` 一起编译：`g++ test_override.cc src/common/hcomm_dlsym/hccl_dl.cc -ldl -o test_override`，运行观察输出。

**需要观察的现象**：只要进程里出现对 `HcclDlopen` 的调用，就会打印 `[stub] ... intercepted`，证明强符号覆盖了弱别名。

**预期结果**：链接器选中测试文件的强符号版本；这正是插桩工具接管 HCCL 动态加载的原理。能否在完整 HCCL 场景下复现，待本地验证（需要 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接把 `dlopen` 声明成可覆盖，而要先包一层 `HcclDlopen`？

**答案**：系统 `dlopen` 是 libc 的强符号，HCCL 无法把它变弱；强行拦截系统符号需要 `LD_PRELOAD` 且影响全进程。包一层后，`__HcclDlopen` 是 HCCL 自己的真实实现，`HcclDlopen` 是 HCCL 自己声明的弱别名，覆盖范围精确限定在"谁加载了 hccl 的这层封装"，可控且无需改环境。

**练习 2**：`weak_alias(__HcclDlopen, HcclDlopen)` 中，`__typeof` 起什么作用？

**答案**：让别名变量自动继承真实函数的完整类型（返回值 + 参数列表），避免手写签名时出现不一致导致的未定义行为。

**练习 3**：这三个封装为什么必须放 `extern "C"`？

**答案**：`dlsym` 按字符串查符号；C++ 会修饰函数名（如 `_Z10HcclDlopenPKci`），外部程序想覆盖时也得猜修饰名。`extern "C"` 保证符号表里就是 `HcclDlopen` 这个可预测的名字。

### 4.2 模块二：HcommDlInit 入口与 dlopen libhcomm.so

#### 4.2.1 概念说明

`HcommDlInit` 是整个解耦通道的总入口：它把 `libhcomm.so` 加载进进程，并把句柄分发给 11 个按域划分的子模块（资源、拓扑、原语、CCU 等），让它们各自完成符号绑定。它回答三个问题：

1. **什么时候绑定？** 进程加载 libhccl.so 时——通过 `__attribute__((constructor))` 构造函数触发，任何算子被调用前已完成。
2. **绑定谁？** 每个 `XxxDlInit` 只绑定自己域的符号，互不交叉。
3. **绑定失败怎么办？** 符号不存在时置支持标志为 false、函数落回弱符号兜底，而不是崩溃——这正是两仓独立演进、版本可超前可滞后的容错基础。

#### 4.2.2 核心流程

```text
libhccl.so 被进程加载
   │  __attribute__((constructor)) InitCompat()          (compat.cc)
   ▼
pthread_once ──▶ HcommDlInit()                            (hcomm_dlsym.cc)
   │
   ├─ gLibHandle 非空? ── 是 ──▶ 直接返回（幂等）
   │
   ├─ gLibHandle = HcclDlopen("libhcomm.so", RTLD_NOW)
   │       失败 ──▶ stderr 打印 dlerror，返回（后续所有弱符号均为兜底）
   │
   ├─ dlerror() 清理错误缓存
   ▼
按域依次绑定（句柄透传给各 XxxDlInit）：
   HcclResDlInit / HcclRankGraphDlInit / HcommPrimitivesDlInit /
   HcclInnerDlInit / HcommProfilingDlInit / HcclCommDlInit /
   HcclResExptDlInit / CcuResDlInit / HcclCcuResDlInit /
   CcuLaunchDlInit / CcuPrimitivesImplDlInit
```

#### 4.2.3 源码精读

[点击查看 compat.cc:L15-L24](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/compat.cc#L15-L24)：库构造函数 `InitCompat` 用 `static pthread_once_t` 保证 `HcommDlInit` 在多线程下也只执行一次——dlsym 绑定是进程级一次性动作。另一个调用点是 [aicpu_task_cache_clear.cc:L110](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aicpu_task_cache_clear.cc#L110)，在清理 Task Cache 前再兜底触发一次（幂等，重复调用无害）。

[点击查看 hcomm_dlsym.cc:L63-L72](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L63-L72)：`HcommDlInit` 开头两步——`gLibHandle` 判空实现幂等；随后 `HcclDlopen("libhcomm.so", RTLD_NOW)` 加载 HCOMM。注意这里用的就是模块一的新封装，而不是裸 `dlopen`（本提交把旧代码的直接 `dlopen` 全部替换掉了）。加载失败只打 stderr 不 abort，进程继续以"全兜底"模式运行，调用任何 HCOMM 能力时会得到明确错误日志。

[点击查看 hcomm_dlsym.cc:L74-L87](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L74-L87)：`dlerror()` 清掉残留错误信息后，把句柄依次传给 11 个域级 `XxxDlInit`。各域职责在下一讲（u6-l2）展开，本讲只需记住"一个域一个文件、一个入口函数"的组织方式。

版本探测也走同一文件：

[点击查看 hcomm_dlsym.cc:L32-L42](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L32-L42)：`GetHcommVersion` 通过 CANN 的 `aclsysGetVersionNum("hcomm", ...)` 查询已安装 HCOMM 的版本号并缓存在静态变量中（0 表示未知）。u2-l2 讲过的"版本闸门 `GetHcommVersion() < 9.0.0` 回退老流程"就是消费这个值。

[点击查看 hcomm_dlsym.cc:L44-L60](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L44-L60)：`HcommIsProfilingSupported/HcommIsExportThreadSupported` 展示了典型的"版本号 + 支持标志"双重判定——能力是否可用，既要 HCOMM 够新，又要符号真的绑定成功（后者由 `HcommIsSupportHcclThreadExportToCommEngine()` 查询，见模块三）。

#### 4.2.4 代码实践

**实践目标**：跟踪 `HcommDlInit` 的两个触发点，理解"为什么 HCCL 用户从不需要手动调初始化"。

**操作步骤**：

1. 在仓库内搜索 `HcommDlInit` 的全部调用点（用 Grep）。
2. 对每处调用，向上追一层：构造函数在什么时机执行？`aicpu_task_cache_clear` 里的调用为什么安全（提示：幂等）。
3. 在 `HcclResDlInit`、`HcommPrimitivesDlInit` 等任一函数入口加一行 `HCCL_COMPAT_DEBUG` 日志（仅本地实验，不要提交）。

**需要观察的现象**：任何一个 HCCL 算子第一次被调用时，日志中应已出现各域绑定记录——证明绑定发生在算子调用之前（构造函数阶段）。

**预期结果**：两个调用点都收敛到同一份 `gLibHandle`，第二次调用不产生重复绑定日志。具体日志输出待本地验证（需装有 HCOMM 的 CANN 环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么选择 `RTLD_NOW` 而不是 `RTLD_LAZY`？

**答案**：`RTLD_NOW` 在加载时立即解析全部符号，库与库不匹配在 dlopen 瞬间报错；`RTLD_LAZY` 会把失败推迟到首次函数调用，对通信库而言，错误暴露在训练中段的代价远高于加载时。

**练习 2**：`HcommDlInit` 加载失败后进程为什么还能继续跑？

**答案**：失败只置空 `gLibHandle` 并打日志；所有跨仓接口都是弱符号兜底（打印 "not supported" 并返回 -1），HCCL 自身代码不引用任何 HCOMM 链接期符号，所以不会因缺库而无法启动——代价是相关能力全部不可用。

**练习 3**：`dlerror()` 调用（L74）清的是什么？

**答案**：`dlerror()` 既返回最近一次动态库错误，又把内部错误状态清空。清空之后，后续 `HcclDlsym` 若失败，`dlerror` 的返回值可准确归属，不会被之前残留的错误污染。

### 4.3 模块三：dlsym_common.h 宏工具箱（DEFINE_WEAK_FUNC / INIT_SUPPORT_FLAG）

#### 4.3.1 概念说明

这是本讲的核心模式。HCCL 每接入一个 HCOMM 符号，都要在四个位置写四行模式化代码，`dlsym_common.h` 的三个宏把这四个位置全部模板化：

| 步骤 | 宏/函数 | 所在文件 | 作用 |
| --- | --- | --- | --- |
| ① 声明弱函数 | `DECL_WEAK_FUNC` | `hccl_res_dl.h` 等域头文件 | 告诉 HCCL 调用方"这个函数存在、签名如此" |
| ② 定义兜底桩 | `DEFINE_WEAK_FUNC` | `hccl_res_dl.cc` 等域实现文件 | 提供弱定义：未绑定时打印错误并返回 -1；同时生成支持标志 getter |
| ③ 声明标志查询 | `DECL_SUPPORT_FLAG` | 域头文件 | 让其他模块能问"这个能力有没有绑定成功" |
| ④ 运行期绑定 | `INIT_SUPPORT_FLAG` | 域 `XxxDlInit` 函数内 | 用 `HcclDlsym` 查符号，成功置 true、失败置 false |

#### 4.3.2 核心流程

以"接入符号 `HcclDevMemAcquire`"为例的静态/动态两条线：

```text
编译期（静态）：
  DECL_WEAK_FUNC(HcclResult, HcclDevMemAcquire, ...)
      └─ 生成弱声明：调用方代码可直接写 HcclDevMemAcquire(...)
  DEFINE_WEAK_FUNC(HcclResult, HcclDevMemAcquire, ...)
      ├─ static bool g_HcclDevMemAcquireSupported = false;
      ├─ extern "C" bool HcommIsSupportHcclDevMemAcquire() { return g_...; }
      └─ 弱定义：{ HCCL_COMPAT_ERROR("not supported"); return (HcclResult)(-1); }

运行期（动态，HcommDlInit 触发）：
  HcclResDlInit(gLibHandle)
      └─ INIT_SUPPORT_FLAG(handle, HcclDevMemAcquire)
            ├─ ptr = HcclDlsym(handle, "HcclDevMemAcquire")
            ├─ ptr != nullptr ? 标志置 true : 标志置 false（打 DEBUG 日志）
            └─ 注意：dlsym 返回的地址并未被"替换"进弱符号——
               dlsym 拿到的是 libhcomm 内部地址，仅用于探测存在性
```

一个容易误解的细节要澄清：`INIT_SUPPORT_FLAG` 里 `dlsym` 拿到的函数指针**只用来判断符号是否存在**，并不把弱符号替换成真实函数。真正的调用解析发生在最终链接：HCCL 主库与 libhcomm.so 同时提供 `HcclDevMemAcquire` 时，强符号（HCOMM 侧）覆盖 HCCL 的弱桩——弱符号机制本身完成了"有则用真、无则用桩"。

#### 4.3.3 源码精读

[点击查看 dlsym_common.h:L152-L162](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L152-L162)：三个关键宏。`DECL_WEAK_FUNC` 展开为带 `__attribute__((weak))` 的声明；`DEFINE_WEAK_FUNC` 一次生成三件套——`static bool g_<fn>Supported` 支持标志、`HcommIsSupport<fn>()` 查询函数、打印 `HCCL_COMPAT_ERROR` 并返回 `(type)(-1)` 的弱定义。注意本提交把宏内的 `dlsym` 换成了 `HcclDlsym`（见下条）。

[点击查看 dlsym_common.h:L166-L175](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L166-L175)：`INIT_SUPPORT_FLAG(handle, func_name)`——注意它内部调用的是 **`HcclDlsym`**（新封装）而非裸 `dlsym`，这是本轮改动在宏层面的落点：连同 [dlsym_common.h:L32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L32) 新增的 `#include "hccl_dl.h"`，整个绑定层都从系统调用迁移到了弱符号封装上，意味着外部对 `HcclDlsym` 的强符号覆盖会同步接管所有符号探测。

[点击查看 dlsym_common.h:L164](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L164)：`DECL_SUPPORT_FLAG` 展开为 `extern "C" bool HcommIsSupport<fn>(void)` 的声明，供跨模块查询。

再以"资源域"为完整样例看四步的真实用法：

[点击查看 hccl_res_dl.h:L78-L103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h#L78-L103)：11 个资源类接口的 `DECL_WEAK_FUNC` 声明（获取远端 IPC 缓冲、Task 注册、设备内存申请、线程导出等），其中 `HcclThreadExportToCommEngine/HcclThreadAcquireWithConfig/HcclDedicatedThreadAcquire` 还配了 `DECL_SUPPORT_FLAG`——这三种能力版本差异大，调用前必须先查询。

[点击查看 hccl_res_dl.cc:L17-L38](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc#L17-L38)：与头文件一一对应的 `DEFINE_WEAK_FUNC` 桩定义，注释明确"签名与真实 API 完全一致"——签名一致是强符号覆盖成立的前提。

[点击查看 hccl_res_dl.cc:L41-L53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc#L41-L53)：`HcclResDlInit` 对全部 11 个符号执行 `INIT_SUPPORT_FLAG`，逐个置支持标志。

此外 [dlsym_common.h:L14-L22](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L14-L22) 的 `CANN_VERSION` 宏族与 [L38-L113](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L38-L113) 的兼容桩（`HcclCommStatus/HcclP2pKernelParam` 等）服务于另一维度的解耦——新旧 CANN 版本间的编译兼容，与运行期 dlsym 解耦正交，了解即可。

#### 4.3.4 代码实践

**实践目标**：为假想的 HCOMM 新符号 `HcclNewFunction(HcclComm comm, uint64_t param)` 写出完整的四步接入骨架（本讲的综合练习，写在纸面或临时文件，**不要修改仓库源码**）。

**操作步骤**：

1. **第一步 `DECL_WEAK_FUNC`** —— 在 `hccl_res_dl.h` 的 `extern "C"` 块内加声明（示例代码）：

```cpp
// hccl_res_dl.h 新增（示例代码）
DECL_WEAK_FUNC(HcclResult, HcclNewFunction, HcclComm comm, uint64_t param);
DECL_SUPPORT_FLAG(HcclNewFunction); // 若能力需按版本查询
```

2. **第二步 `DEFINE_WEAK_FUNC`** —— 在 `hccl_res_dl.cc` 加桩定义（示例代码）：

```cpp
// hccl_res_dl.cc 新增（示例代码）
DEFINE_WEAK_FUNC(HcclResult, HcclNewFunction, HcclComm comm, uint64_t param);
```

   宏会自动生成 `g_HcclNewFunctionSupported`、`HcommIsSupportHcclNewFunction()` 和兜底实现。

3. **第三步 `INIT_SUPPORT_FLAG`** —— 在 `HcclResDlInit` 末尾加一行（示例代码）：

```cpp
INIT_SUPPORT_FLAG(libHcommHandle, HcclNewFunction);
```

4. **第四步（绑定入口）**：`HcclNewFunction` 属于资源域，绑定入口 `HcclResDlInit` 已由 [hcomm_dlsym.cc:L76](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L76) 驱动，`hcomm_dlsym.cc` **无需改动**；只有全新开一个域时才需要在 `HcommDlInit` 里新增一行 `XxxDlInit(gLibHandle)`。

5. 调用方使用模式（示例代码）：

```cpp
if (HcommIsSupportHcclNewFunction()) {
    CHK_RET(HcclNewFunction(comm, param));
}
```

**需要观察的现象**：对照 `HcclDedicatedThreadAcquire` 的既有四处代码，逐行核对骨架是否一致。

**预期结果**：骨架与仓库既有模式逐字对齐；老版本 HCOMM 缺该符号时，调用得到一条 `[HcclWrapper] HcclNewFunction not supported` 错误日志与返回值 -1，而非崩溃。编译验证待本地进行。

#### 4.3.5 小练习与答案

**练习 1**：`DEFINE_WEAK_FUNC` 生成的兜底实现返回 `(type)(-1)`，对 `HcclResult` 而言意味着什么？调用方为何仍必须判返回值？

**答案**：`HcclResult` 是整型枚举，-1 不落在任何合法值域，是明确的"未支持"信号。弱桩已把错误打进日志，调用方用 `CHK_RET` 上抛即可；若不判，未支持场景的错误会被静默吞掉。

**练习 2**：`INIT_SUPPORT_FLAG` 中的 `#func_name` 起什么作用？

**答案**：把函数名变成字符串字面量，让 `HcclDlsym(handle, "HcclNewFunction")` 与 DEBUG 日志里的名字永远和被绑定的函数同名，避免手写字符串的三处不同步。

**练习 3**：支持标志为什么用 `static bool` 全局变量 + getter 函数，而不是直接暴露变量？

**答案**：变量 `static` 限制在本编译单元，跨模块只能经 `extern "C"` 的 getter 访问——封装防止外部误改，getter 的 C 链接又保证任何编译单元（含 HCOMM 侧）都能用统一名字查询。

## 5. 综合实践

**任务：画出一次完整的"HCOMM 能力探测"时序并回答覆盖问题。**

1. 画出从 `libhccl.so` 被加载，到 `HcommIsExportThreadSupported()` 返回 true/false 的完整时序，标注：构造函数、`pthread_once`、`HcclDlopen`、`HcclResDlInit`、`INIT_SUPPORT_FLAG(HcclThreadExportToCommEngine)`、`GetHcommVersion` 各步骤（涉及 [compat.cc:L20-L24](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/compat.cc#L20-L24)、[hcomm_dlsym.cc:L63-L87](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L63-L87)、[hccl_res_dl.cc:L47](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc#L47)）。
2. 回答三个递进问题：
   - HCOMM 版本 9.0.0 但未导出 `HcclThreadExportToCommEngine` 符号时，`HcommIsExportThreadSupported()` 返回什么？为什么？
   - 若某插桩工具强符号覆盖了 `HcclDlsym` 并对特定函数名返回 nullptr，会对支持标志产生什么影响？这体现了封装层怎样的用途？
   - 若没有 `pthread_once`，两个线程同时首次触发构造函数逻辑会发生什么？
3. 参考 `HcommIsExportThreadSupported` 的双重判定（版本 ≥ 9.0.0 **且** 支持标志为 true），为一个假想能力 `HcclNewFunction` 写出同款的 `HcommIsNewFunctionSupported()` 判定函数（示例代码，标注清楚不提交）。

预期产出：一张时序图 + 三段文字回答 + 一个判定函数。运行时序的日志验证待本地进行。

## 6. 本讲小结

- HCCL 与 HCOMM 通过 **dlopen + dlsym + 弱符号** 三件套实现两仓独立编译演进：编译期 HCCL 只依赖自己写的弱桩，运行期 HCOMM 的强符号自然覆盖弱桩，缺失时弱桩兜底报错。
- 绑定总入口 `HcommDlInit` 由 **库构造函数 + pthread_once** 保证在任何算子调用前恰好执行一次：`HcclDlopen("libhcomm.so", RTLD_NOW)` 后把句柄分发给 11 个域级 `XxxDlInit`。
- 接入一个 HCOMM 符号的固定四步：**`DECL_WEAK_FUNC`（声明）→ `DEFINE_WEAK_FUNC`（桩 + 支持标志）→ `INIT_SUPPORT_FLAG`（dlsym 探测）→ `XxxDlInit`（绑定入口）**，支持标志经 `HcommIsSupport<fn>()` 查询。
- 本轮新增 `hccl_dl.h/.cc`：用 glibc 风格 **weak_alias** 把 `dlopen/dlsym/dlclose` 封装成弱符号 `HcclDlopen/HcclDlsym/HcclDlclose`，`HcommDlInit`、`INIT_SUPPORT_FLAG` 及各 `_dl` 封装已全部迁移到新入口，外部可用强符号接管 HCCL 的全部动态加载（插桩/Mock）。
- `INIT_SUPPORT_FLAG` 中的 `HcclDlsym` 仅探测符号存在性；真正的"有则用真、无则用桩"由弱符号链接机制完成，二者不要混淆。
- 版本维度的兼容（`CANN_VERSION` 宏、类型桩）与运行期 dlsym 解耦是正交的两套机制，同住在 `dlsym_common.h`。

## 7. 下一步学习建议

下一讲 **u6-l2 资源、原语与拓扑的 dlsym 封装** 将深入 11 个域模块本身：`hccl_res_dl`（资源获取）、`hcomm_primitives_dl`（Write/Read/Reduce/Notify 数据面原语）、`hccl_rank_graph_dl`（拓扑与 Thread/Channel 资源）等，重点跟踪一次 template `KernelRun → channel → 原语` 的跨仓调用。建议先自行浏览 `src/common/hcomm_dlsym/` 目录下各 `_dl` 文件，数一数每个文件里 `DEFINE_WEAK_FUNC` 的数量，感受"域"的粒度划分。
