# 算子运行时执行流程：ACL 与 Device 调用

## 1. 本讲目标

前几讲我们学会了「如何用 `Compute()` 写一行表达式」「如何用三级 Builder 把表达式组装成 `DeviceOp`」。但我们一直回避了一个关键问题：

> 当我们在 `main()` 里写下 `deviceOp.Run(arguments, stream)` 之前，到底要准备些什么？这一行背后，算子是怎么真正在昇腾硬件上跑起来的？

本讲就以 `muls`（矩阵标量乘法）样例为模板，把一个 ATVOSS 算子**在 Host 侧从启动到结束的完整运行时流程**拆成 10 个步骤讲清楚。学完后你应当能够：

1. 看懂并默写基于 ACL（Ascend Computing Language）的标准算子调用样板代码：`aclInit` → `aclrtSetDevice` → `aclrtCreateContext` → `aclrtCreateStream` → `aclrtMalloc` → `aclrtMemcpy` → …… → `aclFinalize`。
2. 理解 `examples/common/example_common.h` 提供的通用工具：`CHECK_ACL_RET`、`ReleaseSource`（RAII 释放）、`IsClose` / `VerifyResults`（精度校验）。
3. 学会用 `Atvoss::Tensor` 包装设备指针、用 `Atvoss::ArgumentsBuilder` 链式构造入参，并调用 `deviceOp.Run(arguments, stream)` 真正执行算子。
4. 把这套流程套用到任意一个新算子样例上。

> 说明：本讲只讲 **Host 侧的运行时调用流程**，不深入 `DeviceAdapter::Run` 内部如何做 Tiling 与 Kernel Launch（那是进阶篇 u2-l7 的主题）。本讲把 `deviceOp.Run(...)` 当作一个「黑盒入口」来使用。

## 2. 前置知识

阅读本讲前，你应当已经掌握（来自 u1-l1 ~ u1-l4）：

- ATVOSS 的定位：基于 Ascend C 的 Vector 算子模板库（u1-l1）。
- 如何用 `scripts/build.sh` 编译并运行样例（u1-l2）。
- 五层架构 Device > Kernel > Block > Tile > Basic（u1-l3）。
- 用户编程模型：`Config` 结构体、`Compute()` 里的 `PlaceHolder` 与表达式、三级 Builder 组装出 `DeviceOp`（u1-l4）。

本讲会用到几个昇腾运行时（Runtime）基础概念，先用一句话解释：

| 术语 | 通俗解释 |
|------|----------|
| **ACL** | Ascend Computing Language，昇腾算子运行时的 C 语言接口集合（`acl*.h`），是 Host 程序操控 NPU 的最底层入口。 |
| **Device** | 这里指 NPU 设备（不是五层架构里的 Device 层）。一个 Host 进程通过 `deviceId` 选中一张 NPU 卡。 |
| **Context** | 设备上下文，一个执行环境容器，管理 Stream 等资源。 |
| **Stream** | 任务流，算子被异步提交到 Stream 上，按提交顺序执行。 |
| **Host / Device 内存** | Host 指 CPU 侧内存（普通内存），Device 指 NPU 侧显存。算子输入要先搬到 Device 显存，算完后结果再搬回 Host。 |
| **GM（Global Memory）** | NPU 上的全局显存，Host 通过 `aclrtMalloc` 申请、通过 `aclrtMemcpy` 搬运，就是上面的「Device 内存」。 |
| **异步执行** | `deviceOp.Run(...)` 把任务**提交**到 Stream 后立即返回，计算可能还没完成；需要 `aclrtSynchronizeStream` 等它真正算完。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `examples/muls/muls.cpp` | **主样本**。包含完整的 `Run()` 函数，10 步 ACL 流程的权威实现都在这里。 |
| `examples/abs/abs.cpp` | abs 样例的完整实现，结构与 muls 一致，是本讲代码实践的对象。 |
| `examples/common/example_common.h` | 通用工具：`CHECK_ACL_RET` 宏、`ReleaseSource`（RAII 守卫）、`IsClose` / `VerifyResults`（精度校验）。所有样例都 `#include` 它。 |
| `include/utils/tensor.h` | `Atvoss::Tensor<T>` 定义：用「设备指针 + 形状」描述一个算子入参。 |
| `include/utils/arguments/arguments.h` | `Atvoss::ArgumentsBuilder` 定义：链式把多个 `Tensor` / scalar 打包成 `deviceOp.Run` 需要的 `arguments`。 |
| `docs/tutorials/developer_guide.md` | 官方开发指南，以 muls 为例逐步讲解，本讲与之对应。 |
| `include/elewise/device/device_adapter.h` | `DeviceAdapter::Run` 的内部实现（本讲只看它的入口签名，深入留到 u2-l7）。 |

## 4. 核心概念与源码讲解

本讲对应 4 个最小模块：

- **4.1 ACL 资源初始化与 RAII 释放**：如何打开/关闭 NPU 运行环境。
- **4.2 Host↔Device 数据搬运**：输入怎么搬上去、结果怎么搬回来。
- **4.3 Tensor 构造与 ArgumentsBuilder 调用**：把裸指针包装成 ATVOSS 入参。
- **4.4 精度校验 IsClose / VerifyResults**：判断算子算得对不对。

这 4 个模块合起来，正好是 muls 样例 `Run()` 函数的 10 个步骤（见下图）。我们把整张时序先记在脑海里，再逐段拆解：

```
aclInit ──► aclrtSetDevice ──► aclrtCreateContext ──► aclrtCreateStream   (步骤 1~4：搭环境)
   │
   ▼
aclrtMalloc(in/out) ──► aclrtMemcpy(Host→Device)                          (步骤 5~6：备数据)
   │
   ▼
Atvoss::Tensor + ArgumentsBuilder.build() ──► deviceOp.Run(arguments,stream)  (步骤 7~8：跑算子)
   │
   ▼
aclrtSynchronizeStream ──► aclrtMemcpy(Device→Host)                       (步骤 9：取结果)
   │
   ▼
VerifyResults(golden, hostOutput)                                         (步骤 10：校验)
   │
   ▼  （函数返回，RAII 守卫按 LIFO 逆序析构）
aclrtFree → aclrtDestroyStream → aclrtDestroyContext → aclrtResetDevice → aclFinalize
```

---

### 4.1 ACL 资源初始化与 RAII 释放

#### 4.1.1 概念说明

任何要用 NPU 的 Host 程序，都必须先「打开设备、建立执行环境」。ACL 规定了一套固定的资源申请顺序：

1. `aclInit`：初始化 ACL 全局运行时（整个进程只调一次）。
2. `aclrtSetDevice(deviceId)`：选中第几张 NPU 卡。
3. `aclrtCreateContext`：在该设备上创建一个上下文。
4. `aclrtCreateStream`：在上下文里创建一条任务流，算子后续提交到这条流上。

这些资源用完后**必须按相反顺序释放**：先销毁 Stream，再销毁 Context，再 ResetDevice，最后 `aclFinalize`。如果顺序错了（比如还没销毁 Stream 就 `aclFinalize`），会报错。

手工管理这套「申请—释放」配对很容易漏写。ATVOSS 样例用一个极简的 **RAII（Resource Acquisition Is Initialization，资源获取即初始化）守卫**来兜底：每个资源申请完，立刻挂一个「作用域结束时自动调用释放函数」的守卫对象。函数返回时，C++ 栈对象按构造的**逆序**析构，恰好就是正确的释放顺序。

#### 4.1.2 核心流程

资源初始化与释放的配对关系如下：

| 申请（按此顺序） | 对应释放 | RAII 守卫变量名 |
|------------------|----------|------------------|
| `aclInit` | `aclFinalize` | `finalizeGuard` |
| `aclrtSetDevice` | `aclrtResetDevice` | `deviceResetGuard` |
| `aclrtCreateContext` | `aclrtDestroyContext` | `contextDestroyGuard` |
| `aclrtCreateStream` | `aclrtDestroyStream` | `streamDestroyGuard` |

因为 C++ 栈对象的析构顺序是**后构造的先析构**（LIFO，后进先出），所以这 4 个守卫在函数末尾会以 `streamDestroy → contextDestroy → deviceReset → finalize` 的顺序触发——正好是 ACL 要求的正确释放顺序。**这是 RAII 设计的一个精妙之处：你只要按申请顺序声明守卫，正确的释放顺序就「免费」得到了。**

伪代码：

```
aclInit()
guard_finalize = RAII(aclFinalize)       // 最后才析构
aclrtSetDevice()
guard_device   = RAII(aclrtResetDevice)
aclrtCreateContext()
guard_context  = RAII(aclrtDestroyContext)
aclrtCreateStream()
guard_stream   = RAII(aclrtDestroyStream)
... 函数体 ...
return
// 析构（LIFO）：stream → context → device → finalize
```

#### 4.1.3 源码精读

RAII 守卫的实现只有十几行，定义在通用头文件里。`AclResourceGuard<F>` 在析构函数里调用保存的可调用对象 `f`；`ReleaseSource` 是一个推导辅助函数，让你用 `ReleaseSource([](){ ... })` 一行就造出一个守卫：

[examples/common/example_common.h:L58-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L58-L71) —— RAII 守卫：构造时保存释放函数，析构时自动调用。

```cpp
template <typename F>
struct AclResourceGuard {
    F f;
    ~AclResourceGuard() { f(); }   // 作用域结束自动释放
};

template <typename F>
AclResourceGuard<F> ReleaseSource(F&& f) {
    return AclResourceGuard<F>{std::forward<F>(f)};
}
```

错误检查宏 `CHECK_ACL_RET`：每个 ACL 调用都返回 `aclError`，宏在出错时打印日志并 `return`，避免在错误状态下继续往下走：

[examples/common/example_common.h:L24-L31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L24-L31) —— ACL 返回值检查宏。

muls 样例中步骤 1~4 的真实代码，每一步都是「一句 ACL 调用 + 一个 RAII 守卫」的固定写法：

[examples/muls/muls.cpp:L136-L153](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L136-L153) —— muls 的 ACL 环境初始化 4 步。

```cpp
// --- Step 1: ACL 初始化 ---
CHECK_ACL_RET(aclInit(nullptr));
auto finalizeGuard = ReleaseSource([]() { aclFinalize(); });

// --- Step 2: 设置 device ID ---
const int32_t deviceId = 0;
CHECK_ACL_RET(aclrtSetDevice(deviceId));
auto deviceResetGuard = ReleaseSource([deviceId]() { aclrtResetDevice(deviceId); });

// --- Step 3: 创建 Context ---
aclrtContext context = nullptr;
CHECK_ACL_RET(aclrtCreateContext(&context, deviceId));
auto contextDestroyGuard = ReleaseSource([context]() { aclrtDestroyContext(context); });

// --- Step 4: 创建 Stream ---
aclrtStream stream = nullptr;
CHECK_ACL_RET(aclrtCreateStream(&stream));
auto streamDestroyGuard = ReleaseSource([stream]() { aclrtDestroyStream(stream); });
```

> 注意守卫变量名的声明顺序：`finalize` → `device` → `context` → `stream`。函数返回时按 LIFO 析构，得到 `stream → context → device → finalize` 的正确释放顺序。

#### 4.1.4 代码实践

**实践目标**：亲手验证 RAII 的 LIFO 析构顺序。

**操作步骤**：

1. 打开 [examples/common/example_common.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L58-L71)，确认 `AclResourceGuard` 的析构函数里调用了 `f()`。
2. 在 muls 的 `Run()` 函数里（[examples/muls/muls.cpp:L136-L153](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L136-L153)），把 4 个 `ReleaseSource(...)` 的 lambda 体里各加一行 `std::cout << "destroy xxx" << std::endl;`（这只是观察用的日志，**不要修改任何源码逻辑，观察完请还原**）。
3. 编译运行（参考 u1-l2）：`bash scripts/build.sh -DSOC=ascend950 muls`，然后 `output/bin/muls --shape=32,32`。

**需要观察的现象**：程序退出阶段，日志会按 `stream → context → device → finalize` 的顺序打印，而不是声明顺序。

**预期结果**：析构顺序与声明顺序相反，证明 LIFO。**待本地验证**（本环境无 NPU，需在真实昇腾环境或 cannsim 仿真下运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `finalizeGuard` 声明在 `streamDestroyGuard` 之后（即先 SetDevice/Context/Stream，最后才 aclInit），会发生什么？为什么实际代码一定要把 `aclInit` 放在最前面？

> **答案**：`aclInit` 是整个 ACL 运行时的入口，必须最先调用，否则 `aclrtSetDevice` 等接口不可用。同时它对应的 `aclFinalize` 必须最后调用（其它资源要先释放完），而 LIFO 析构要求「最后析构的对象最先声明」，所以 `finalizeGuard` 必须声明在最前。

**练习 2**：`CHECK_ACL_RET` 宏在出错时执行 `return;`，这意味着它只能用在返回类型为 `void` 的函数里。`Run()` 函数的返回类型是什么？为什么这里用 `return;` 是安全的？

> **答案**：`Run()` 的返回类型是 `void`（见 [examples/muls/muls.cpp:L133-L134](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L133-L134)）。出错时直接 `return;` 会触发栈上 RAII 守卫析构，自动释放已申请的资源，因此是安全的；代价是错误信息只打印到 stderr，不向调用方传递错误码。

---

### 4.2 Host↔Device 数据搬运

#### 4.2.1 概念说明

NPU 算子只能直接读写 **Device 显存（GM）**，不能直接读写 Host 内存。所以执行算子前，必须把 Host 上的输入数据搬到 Device；算完后，再把 Device 上的结果搬回 Host 才能打印或校验。

ACL 提供两个核心搬运接口：

- `aclrtMalloc(void**, size, policy)`：在 Device 显存上申请一段内存，返回设备指针。
- `aclrtMemcpy(dst, dstSize, src, srcSize, kind)`：在 Host↔Device 之间拷贝，`kind` 指明方向（`ACL_MEMCPY_HOST_TO_DEVICE` 或 `ACL_MEMCPY_DEVICE_TO_HOST`）。

muls 样例用 `ACL_MEM_MALLOC_HUGE_FIRST` 申请策略，优先使用大页（Huge Page）显存，对大块连续搬运更友好。

#### 4.2.2 核心流程

数据搬运分三个阶段，贯穿步骤 5、6、9：

```
[Host]                    [Device 显存 GM]              [Host]
hostInput  --Step6 搬上去-->  deviceInput
                              deviceOutput  <--算子写入-- deviceOp.Run
              Step9 搬回来-->  hostOutput
```

步骤 5 先 `aclrtMalloc` 申请输入/输出两块 GM；步骤 6 把 Host 输入拷到 GM 输入区；算子运行后，步骤 9 把 GM 输出区拷回 Host。

数据量按形状元素总数 × 每个元素字节数计算：

\[ \text{shapeSize} = \prod_{i} \text{shape}_i ,\qquad \text{byteSize} = \text{shapeSize} \times \text{sizeof}(T) \]

#### 4.2.3 源码精读

步骤 5：在 Device 上申请输入/输出显存，同样配 RAII 守卫自动 `aclrtFree`：

[examples/muls/muls.cpp:L155-L167](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L155-L167) —— 申请输入/输出 GM。

```cpp
const size_t shapeSize = std::accumulate(shape.begin(), shape.end(), size_t{1}, std::multiplies<>{});
const size_t inputSize  = shapeSize * sizeof(TensorDtype);
const size_t outputSize = shapeSize * sizeof(ScalarDtype);
void* rawInput = nullptr;
CHECK_ACL_RET(aclrtMalloc(&rawInput, inputSize, ACL_MEM_MALLOC_HUGE_FIRST));
auto inputFreeGuard = ReleaseSource([rawInput]() { aclrtFree(rawInput); });
TensorDtype* deviceInput = static_cast<TensorDtype*>(rawInput);
// ... output 同理 ...
```

步骤 6：构造 Host 输入（muls 里填成全 `3.0f`），拷到 Device：

[examples/muls/muls.cpp:L169-L171](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L169-L171) —— Host → Device 拷贝。

```cpp
std::vector<TensorDtype> hostInput(shapeSize, static_cast<TensorDtype>(3.0f));
CHECK_ACL_RET(aclrtMemcpy(deviceInput, inputSize, hostInput.data(), inputSize, ACL_MEMCPY_HOST_TO_DEVICE));
```

步骤 9：先同步 Stream（等算子真正算完），再把结果从 Device 拷回 Host：

[examples/muls/muls.cpp:L192-L195](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L192-L195) —— 同步并拷回结果。

```cpp
CHECK_ACL_RET(aclrtSynchronizeStream(stream));
std::vector<ScalarDtype> hostOutput(shapeSize);
CHECK_ACL_RET(aclrtMemcpy(hostOutput.data(), outputSize, deviceOutput, outputSize, ACL_MEMCPY_DEVICE_TO_HOST));
```

> **关键点**：`aclrtSynchronizeStream` 不能省。`deviceOp.Run(...)` 是**异步提交**，立即返回时算子未必算完；不同步就直接 `aclrtMemcpy` 拷回，可能拷到尚未写满的结果。

#### 4.2.4 代码实践

**实践目标**：理解搬运方向参数 `kind` 的含义与同步的必要性。

**操作步骤**：

1. 在 [examples/muls/muls.cpp:L171](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L171) 与 [L195](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L195) 旁，分别注明两次 `aclrtMemcpy` 的方向：第 6 步是 `HOST_TO_DEVICE`，第 9 步是 `DEVICE_TO_HOST`。
2. 思考：如果把第 9 步的 `aclrtSynchronizeStream(stream)` 注释掉（**仅作思考，不要真改源码**），程序可能在什么情况下给出错误的校验结果？

**需要观察的现象**：两次 memcpy 的方向参数正好相反；同步语句位于「拷回」之前。

**预期结果**：输入走 `HOST_TO_DEVICE`，输出走 `DEVICE_TO_HOST`；同步保证读到的是完整结果。去掉同步后，算子若耗时较长，拷回的 `hostOutput` 可能仍是未初始化/半成品数据，导致精度校验失败。

#### 4.2.5 小练习与答案

**练习 1**：muls 的输入类型是 `TensorDtype`、输出类型是 `ScalarDtype`（在 `Run<int32_t, float>` 时输入 int32、输出 float）。`inputSize` 和 `outputSize` 分别用什么类型计算？为什么不能都用同一个 `inputSize`？

> **答案**：`inputSize = shapeSize * sizeof(TensorDtype)`、`outputSize = shapeSize * sizeof(ScalarDtype)`（见 [L158-L159](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L158-L159)）。当输入 int32（4 字节）输出 float（4 字节）时二者恰好相等，但当类型字节数不同时必须分开算，否则会申请不足或浪费显存。

**练习 2**：`aclrtMemcpy` 的第 2、第 4 个参数都是 `size`，为什么要有两个 size？

> **答案**：第 2 个是目的缓冲区大小 `dstSize`，第 4 个是源缓冲区大小 `srcSize`。同时给出两个 size 让运行时可以做越界保护——拷贝量不会超过任一端缓冲区的容量。

---

### 4.3 Tensor 构造与 ArgumentsBuilder 调用

#### 4.3.1 概念说明

步骤 5、6 之后，我们在 Device 上有了输入/输出的**裸指针**（`deviceInput`、`deviceOutput`）。但 `deviceOp.Run(...)` 不接受裸指针——它需要知道每个参数的**形状**，才能做 Tiling 切分。所以要用 `Atvoss::Tensor<T>` 把「指针 + 形状」打包；再用 `Atvoss::ArgumentsBuilder` 把多个 `Tensor` 和 scalar 按顺序串成一个 `arguments` 对象交给 `deviceOp.Run`。

两个关键点：

- **`Atvoss::Tensor<T>`**：轻量包装类，只保存设备指针和形状数组，本身**不持有内存**（不负责申请/释放，内存的生命周期由步骤 5 的 `aclrtMalloc`/`aclrtFree` 管）。
- **`Atvoss::ArgumentsBuilder`**：链式构造器，`.inputOutput(...)` 接收任意个 `Tensor` 或 scalar，`.build()` 产出最终 `arguments`。它带编译期检查：**只允许 `Tensor` 和标量类型，禁止指针**，避免你误传 Host 指针。

#### 4.3.2 核心流程

```
deviceInput(裸指针) ──┐
                      ├──► Atvoss::Tensor<T>(ptr, shape, dims)
shape[]            ──┘            │
                                   ├──► ArgumentsBuilder{}.inputOutput(in, scalar, out).build()
scalar            ──────────────►        │
                                          └──► arguments (tuple)
deviceOutput(裸指针) + shape ──► Atvoss::Tensor<T> ──┘          │
                                                                 ▼
                                                        deviceOp.Run(arguments, stream)
```

`arguments` 的本质是一个 `std::tuple`：第一项是「输入输出元组」（按 `.inputOutput(...)` 的填写顺序），第二项是「属性元组」（muls 不用属性，为空）。`deviceOp.Run` 内部会从第一项按序取出每个参数。

> **顺序约定（承接 u1-l4）**：`.inputOutput(in, scalar, out)` 里的填写顺序，必须严格对应 `Compute()` 里 `PlaceHolder<N, ...>` 的序号 N：`in` 对应 `PlaceHolder<1>`，`scalar` 对应 `PlaceHolder<2>`，`out` 对应 `PlaceHolder<3>`。填错顺序会算出错误结果。

#### 4.3.3 源码精读

`Atvoss::Tensor<T>` 的构造函数之一，接收「指针 + 形状数组首地址 + 维数」，并把形状拷进内部固定数组（最多 8 维）：

[include/utils/tensor.h:L34-L40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L34-L40) —— 用指针 + 形状数组 + 维数构造 Tensor。

```cpp
Tensor(T* dataPtr, uint64_t* inputShape, size_t dims) : dataPtr_(dataPtr), dims_(dims)
{
    if (dims <= 0 || dims > MAX_DIMS) {
        throw std::runtime_error("input shape dimension is invalid, less than 0 or more than 8");
    }
    std::copy(inputShape, inputShape + dims_, shape_);
}
```

其中 `MAX_DIMS = 8`（[include/utils/tensor.h:L20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L20)），`Tensor` 只保存指针和形状，不管理内存。

`ArgumentsBuilder::inputOutput(...)` 的入口带两条编译期断言，挡住非法参数类型：

[include/utils/arguments/arguments.h:L101-L118](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L101-L118) —— inputOutput 入口与类型约束。

```cpp
struct ArgumentsBuilder {
    template <typename... InitialInputOutput>
    constexpr auto inputOutput(InitialInputOutput&&... inputOutput) const
    {
        // 参数必须为Tensor和非指针类的scalar类型
        static_assert((... && !std::is_pointer_v<InitialInputOutput>),
                      "Pointer types are not allowed in inputOutput parameters");
        static_assert((... && (Util::IsSpecializationOf_v<Atvoss::Tensor, std::decay_t<InitialInputOutput>> ||
                               std::is_scalar_v<std::decay_t<InitialInputOutput>>)),
                      "Only Atvoss::Tensor and scalar types are allowed in inputOutput parameters");
        // ... 组装成 collector ...
    }
```

`.build()` 把「输入输出元组」和「属性元组」打包成一个 tuple 返回：

[include/utils/arguments/arguments.h:L92-L96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/arguments/arguments.h#L92-L96) —— build 产出 tuple。

```cpp
constexpr auto build() const
{
    return std::make_tuple(inOutCollector.inputOutput, attrCollector.attrs);
}
```

muls 样例步骤 7、8 的真实写法：先建两个 `Tensor`，再链式构造 `arguments`，最后按输入类型用 `if constexpr` 选不同的 `DeviceOp` 并 `Run`：

[examples/muls/muls.cpp:L173-L190](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L173-L190) —— 构造 Tensor、ArgumentsBuilder 并执行算子。

```cpp
uint64_t shapeArray[MAX_DIM] = {0};
std::copy(shape.begin(), shape.end(), shapeArray);
Atvoss::Tensor<TensorDtype> in(deviceInput, shapeArray, shape.size());
Atvoss::Tensor<ScalarDtype> out(deviceOutput, shapeArray, shape.size());
float scalar = 3.0f;
auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(in, scalar, out).build();

if constexpr (std::is_same_v<TensorDtype, float>) {
    using DeviceOp = typename MulsConfig<TensorDtype, ScalarDtype>::DeviceOp;
    DeviceOp deviceOp;
    deviceOp.Run(arguments, stream);
} else if constexpr (std::is_same_v<TensorDtype, int32_t>) {
    using DeviceOp = typename MulsConfig<TensorDtype, ScalarDtype>::DeviceOpPromtIn;
    DeviceOp deviceOp;
    deviceOp.Run(arguments, stream);
}
```

`deviceOp.Run(arguments, stream)` 这一行的入口签名（内部三步流程 PrepareParams → CalculateTiling → LaunchKernel 留到 u2-l7 详解）：

[include/elewise/device/device_adapter.h:L97-L98](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L97-L98) —— DeviceAdapter::Run 入口。

```cpp
template <typename Args>
int64_t Run(const Args& arguments, aclrtStream stream = nullptr)
```

> 这里 `if constexpr` 体现的是 muls 的「按输入类型选不同 Compute」模式：float 输入直接相乘（`DeviceOp`）；int32 输入需要先 `Cast` 成 float 再乘（`DeviceOpPromtIn`，见 u2-l10）。这是 ATVOSS 在单个样例里用编译期分支服务多数据类型的典型写法。

#### 4.3.4 代码实践

**实践目标**：体会 `inputOutput(...)` 顺序与 `PlaceHolder` 序号的对应关系。

**操作步骤**：

1. 对照 muls 的 [Compute() 表达式](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L32-L34)（`PlaceHolder<1>=in`、`PlaceHolder<2>=scalar`、`PlaceHolder<3>=out`）与 [步骤 7 的 inputOutput 调用](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L179)（`in, scalar, out`），确认两边顺序一致。
2. 思考实验：如果故意把 `inputOutput(in, scalar, out)` 写成 `inputOutput(out, scalar, in)`（**仅作思考，不要改源码运行**），`PlaceHolder<1>` 现在绑定到谁？算子计算结果会怎样？

**需要观察的现象**：`inputOutput` 参数顺序与 `PlaceHolder` 序号严格一一对应。

**预期结果**：交换后 `PlaceHolder<1>`（标记为 IN 的 `in`）会绑定到 `out` 张量，`PlaceHolder<3>`（标记为 OUT 的 `out`）会绑定到 `in` 张量，导致「从输出区读、往输入区写」，结果错误甚至可能写越界。

#### 4.3.5 小练习与答案

**练习 1**：abs 样例只有两个参数（输入 in、输出 out），没有 scalar。看 [abs.cpp 的 inputOutput 调用](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L175)，它传了几个参数？分别对应哪几个 `PlaceHolder`？

> **答案**：`Atvoss::ArgumentsBuilder{}.inputOutput(t1, t2).build()` 传了 2 个参数。`t1`（输入）对应 `PlaceHolder<1, IN>`，`t2`（输出）对应 `PlaceHolder<2, OUT>`（见 [abs.cpp Compute](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L28-L29)）。

**练习 2**：`ArgumentsBuilder` 的 `static_assert` 为什么禁止指针类型？如果允许传 `float*`，会出什么问题？

> **答案**：裸指针没有形状信息，框架无法对其做 Tiling；而且容易把 Host 指针误当 Device 指针传入，导致算子访问非法地址。强制用 `Atvoss::Tensor` 包装，既保证了形状信息，也语义上明确「这是一个设备张量」。

**练习 3**：`Atvoss::Tensor` 的析构函数（[tensor.h:L50](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L50)）是 `= default`，它会不会释放底层设备内存？

> **答案**：不会。`Tensor` 只保存指针，不持有内存；底层设备内存的释放由步骤 5 的 RAII 守卫（`aclrtFree`）负责。所以 `Tensor` 可以随意复制，不会出现「双重释放」。

---

### 4.4 精度校验 IsClose / VerifyResults

#### 4.4.1 概念说明

算子算完后，怎么知道算得对？最直接的办法：用 CPU 算一份「标准答案」（golden），和 NPU 搬回来的结果逐元素比对。但浮点数有舍入误差，不能简单用 `==` 比较，需要用**带容差的近似相等**（allclose）判定。

`example_common.h` 提供两个工具：

- `IsClose(a, b)`：判断两个 float 是否「近似相等」，结合绝对容差 `ABS_TOL` 和相对容差 `REL_TOL`。
- `VerifyResults(golden, output)`：逐元素调用 `IsClose`，全过返回 `true`，否则打印第一个不匹配的下标并返回 `false`。

#### 4.4.2 核心流程

`IsClose` 的判定逻辑——满足**绝对容差**或**相对容差**之一即认为相等：

\[ \text{close}(a, b) \iff \big( |a - b| \le \text{ABS\_TOL} \big) \;\lor\; \big( |a - b| \le \text{REL\_TOL} \cdot \max(|a|, |b|) \big) \]

其中 `ABS_TOL = 1e-5f`、`REL_TOL = 1e-3f`（见 [example_common.h:L33-L34](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L33-L34)）。

- 绝对容差项：用于接近 0 的小数。
- 相对容差项：用于较大的数，允许误差随数值大小成比例放大。

`VerifyResults` 遍历整个输出向量，遇到第一个不 close 的元素就打印其下标、期望值、实际值并返回 `false`。

#### 4.4.3 源码精读

`IsClose` 实现，`eps` 是一个极小值，防止 `|b|` 为 0 时相对项退化：

[examples/common/example_common.h:L36-L41](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L36-L41) —— 近似相等判定。

```cpp
bool IsClose(float a, float b)
{
    const float eps = 1e-40f;
    float diff = std::abs(a - b);
    return (diff <= ABS_TOL) || (diff <= REL_TOL * std::max(std::abs(a), std::abs(b) + eps));
}
```

`VerifyResults` 是一个模板函数，逐元素比对，并在失败时打印定位信息：

[examples/common/example_common.h:L43-L56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L43-L56) —— 逐元素精度校验。

muls 样例步骤 10 用法：muls 的输入是 `3.0f`、scalar 也是 `3.0f`，所以 golden 是全 `9.0f`：

[examples/muls/muls.cpp:L197-L203](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L197-L203) —— 验证结果。

```cpp
std::vector<ScalarDtype> golden(shapeSize, 9.0f);
if (!VerifyResults(golden, hostOutput)) {
    std::cout << "Accuracy verification failed." << std::endl;
} else {
    std::cout << "Accuracy verification passed." << std::endl;
}
```

成功的标志就是打印 `Accuracy verification passed.`（这也是 u1-l2 提到的样例运行成功标志）。

#### 4.4.4 代码实践

**实践目标**：理解容差判定，并学会为一个新算子准备 golden。

**操作步骤**：

1. 用计算器或心算验证：muls 输入 `3.0f × 3.0f = 9.0f`，所以 [golden](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L198) 是全 9.0f。
2. 对照 abs 样例：输入是全 `-1.5F`（[abs.cpp:L167](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L167)），`Abs(-1.5) = 1.5`，所以 golden 是全 `1.5f`（[abs.cpp:L188](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L188)）。确认这一对应关系。

**需要观察的现象**：每个样例的 golden 都由「输入值 经算子数学公式 计算」得到，是纯 CPU 侧的期望值。

**预期结果**：muls 的 golden=9.0f、abs 的 golden=1.5f，分别与各自算子公式一致。

#### 4.4.5 小练习与答案

**练习 1**：假设某算子输出期望 `10000.0f`，实际 `10005.0f`，`IsClose` 会判定通过吗？（`ABS_TOL=1e-5`、`REL_TOL=1e-3`）

> **答案**：`diff = 5`。绝对项 `5 ≤ 1e-5` 不成立；相对项 `5 ≤ 1e-3 × 10000 = 10` 成立。所以判定通过。说明大数值允许较大的绝对误差。

**练习 2**：`VerifyResults` 遇到第一个不匹配元素就返回 `false`。这种「fail-fast」做法有什么好处和局限？

> **答案**：好处是能立刻定位到第一个出错位置，便于调试；局限是如果后面还有其它出错位置，不会被报告。对于「整体是否通过」的判定已经足够，定位根因时可能需要多次修改输入来逼近。

---

## 5. 综合实践

本讲的综合实践，是把上面 4 个模块串起来，**亲手吃透 abs 样例的完整运行时流程**。

> 重要说明：仓库里的 [examples/abs/abs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp) **已经包含了一个完整的 `Run()` 实现**（步骤 1~10 全齐）。所以本实践是「**源码阅读 + 重建**」型：先读懂它，再脱离源码独立重建，最后做一个小改动观察行为。不要修改源码。

### 实践目标

独立写出 abs 样例「从 `aclInit` 到 `aclFinalize`」的完整调用时序，并理解每一行属于 10 步中的哪一步。

### 操作步骤

1. **通读** [examples/abs/abs.cpp 的 Run() 函数](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L129-L194)（L129~L194），对照本讲的 10 步划分，在纸上给每一行标注它属于哪一步。

2. **重建**：合上源码，在一张白纸（或空白文本文件，**不要写进仓库目录**）上，凭记忆默写出 abs 的调用时序骨架：
   ```
   aclInit → aclrtSetDevice → aclrtCreateContext → aclrtCreateStream
   → aclrtMalloc(输入) / aclrtMalloc(输出)
   → 构造 hostInput(32 个 float)
   → aclrtMemcpy(Host→Device)
   → Atvoss::Tensor(in) / Atvoss::Tensor(out)
   → ArgumentsBuilder{}.inputOutput(in, out).build()
   → deviceOp.Run(arguments, stream)
   → aclrtSynchronizeStream → aclrtMemcpy(Device→Host)
   → VerifyResults(golden, hostOutput)
   → （RAII 析构）aclrtFree → DestroyStream → DestroyContext → ResetDevice → aclFinalize
   ```
   然后与源码核对，补上遗漏的步骤。

3. **小改动预测**：假设要把 abs 的输入从「32 个 `-1.5F`」改成「16 个 `-2.0F` + 16 个 `3.0F`」（即前一半 -2、后一半 3）。请回答：
   - 新的 `golden` 向量应该怎么构造？（提示：`Abs(-2.0)=2.0`，`Abs(3.0)=3.0`）
   - `deviceOp.Run(...)` 这一行需要改动吗？为什么？

4. （可选，**待本地验证**）在有 NPU 的环境下，编译运行：`bash scripts/build.sh -DSOC=ascend950 abs`，再 `output/bin/abs --shape=32`，确认看到 `Accuracy verification passed.`。

### 需要观察的现象

- abs 的 `Run()` 与 muls 的 `Run()` 结构几乎一致，差别只在：abs 没有 scalar、`inputOutput(t1, t2)` 只传 2 个参数、没有 `if constexpr` 多类型分支。
- 改动输入值后，`golden` 必须同步改动，而 `deviceOp.Run(...)` 那一行**完全不用动**——因为算子逻辑由 `Compute()` 里的表达式决定，与具体输入数值无关。

### 预期结果

- 能正确画出 10 步调用时序图，并标出 RAII 的 LIFO 释放顺序。
- 新 golden：前 16 个 `2.0f`、后 16 个 `3.0f`；`deviceOp.Run` 无需改动。**精度校验结果待本地验证**。

## 6. 本讲小结

- 一个 ATVOSS 算子在 Host 侧的运行时流程是固定的 **10 步**：搭环境（`aclInit`/`SetDevice`/`CreateContext`/`CreateStream`）→ 备数据（`Malloc`/`Memcpy` 上行）→ 跑算子（`Tensor` + `ArgumentsBuilder` + `deviceOp.Run`）→ 取结果（`SynchronizeStream`/`Memcpy` 下行）→ 校验（`VerifyResults`）。
- 所有 ACL 资源都用 `ReleaseSource` 造的 **RAII 守卫**托管，靠 C++ 栈的 **LIFO 析构**自动得到正确的释放顺序，无需手写释放代码。
- `Atvoss::Tensor<T>` 只包装「设备指针 + 形状」、不持有内存；`ArgumentsBuilder{}.inputOutput(...).build()` 用编译期断言确保只接受 `Tensor` 和标量，并按填写顺序对应 `PlaceHolder` 序号。
- `deviceOp.Run(arguments, stream)` 是**异步提交**，必须 `aclrtSynchronizeStream` 后才能拷回结果；`deviceOp.Run` 内部走 PrepareParams → CalculateTiling → LaunchKernel 三步（深入留到 u2-l7）。
- 浮点精度用 `IsClose`（绝对容差 OR 相对容差）+ `VerifyResults`（逐元素 fail-fast）判定，成功标志是打印 `Accuracy verification passed.`。
- muls 与 abs 两个样例的 `Run()` 结构几乎一致，掌握一个就能套用到任意新算子。

## 7. 下一步学习建议

本讲把 `deviceOp.Run(...)` 当作黑盒使用。接下来建议：

- **进入进阶篇 u2**：从 [u2-l1 表达式模板基础](u2-l1-expression-template.md) 开始，理解 `Compute()` 里那一行表达式是如何在编译期变成一棵 AST 的。
- **想深入 `deviceOp.Run` 内部**：直接跳到 [u2-l7 Device 层：DeviceAdapter 与算子启动](u2-l7-device-layer.md)，看 `DeviceAdapter::Run` 的 PrepareParams → CalculateTiling → LaunchKernel 三步到底做了什么。
- **想看更多样例**：阅读 `examples/` 下其它样例（如 rms_norm），它们的 `main()` 都遵循本讲的 10 步骨架，差别只在 `Config`/`Compute` 与 `arguments` 的构造。

建议继续阅读的源码：`include/elewise/device/device_adapter.h`（Run 内部）、`include/elewise/device/tiling.h`（Tiling 计算）、以及 `docs/tutorials/developer_guide.md` 的「3.3 运行时执行逻辑」一节（本讲的官方对照版）。
