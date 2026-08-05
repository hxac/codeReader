# 驱动后端与 stub 动态分发

## 1. 本讲目标

本讲承接 u3-l1（运行时公开 API）和 u3-l2（设备/缓冲/内存管理），打开主机运行时的"后端选择器"这只黑盒。读完本讲你应当能够：

1. 说清为什么 `common/` 下的设备/缓冲/内存代码可以一份源码同时服务仿真器与真 FPGA——即"后端无关"是如何用代码实现的。
2. 解释 `libvortex.so`（stub）如何在你第一次调用 `vx_dev_open` 时，用 `dlopen` 按环境变量 `$VORTEX_DRIVER` 把对应的后端库（`libvortex-simx.so` / `libvortex-rtlsim.so` / `libvortex-opae.so` / `libvortex-xrt.so` / `libvortex-gem5.so`）动态加载进来。
3. 掌握 `callbacks_t` 这张"分发表"的契约——每个后端只需实现 6 个 C 函数指针——以及 `callbacks.inc` 模板如何让后端作者几乎不用手写注册代码。
4. 认识 simx / rtlsim / opae / xrt / gem5 五种后端在"同一张契约"下各自的定位与实现差异。

## 2. 前置知识

- **动态库与 `dlopen`/`dlsym`**：Linux 下用 `dlopen("libfoo.so", RTLD_LAZY)` 在运行时把一个 `.so` 加载进进程地址空间，再用 `dlsym(handle, "符号名")` 按名字取出其中的函数地址。Vortex 用这套 POSIX 机制实现"运行时才决定用哪个 GPU 后端"。
- **函数指针表 / 回调表（callback table）**：把一组函数指针放进一个结构体里，当成"接口"传递。调用方只认结构体的字段名，不关心函数内部实现——这是 C 语言里实现"多态/可插拔后端"的经典手法。Vortex 的 `callbacks_t` 就是这样一张表。
- **CP（Command Processor，命令处理器）**：Vortex 设备侧唯一的控制通路与 DMA 引擎。主机从不直接写设备 DRAM，所有命令（拷贝、启动 kernel、栅栏）都打包进一个环（ring），由 CP 取走执行（详见 u3-l2 与 u11-l3）。本讲里你会反复看到"CP register channel"和"CP-visible host memory"这两个词，它们就是后端唯一需要对外暴露的两类能力。
- **HAL（Hardware Abstraction Layer，硬件抽象层）**：把底层硬件差异封装在一组统一接口后面。Vortex 把每个后端都当成一个"纯传输 HAL"。
- **进程内（in-process）仿真**：simx / rtlsim 后端把整个 GPU 当作 C++ 对象跑在和主机同一个进程里，所以"设备内存"其实就是主机的一块普通内存；而 opae / xrt 后端通过 PCIe MMIO 访问真实 FPGA。

## 3. 本讲源码地图

本讲涉及的文件都集中在 `sw/runtime/` 下，按职责分成三层：

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `sw/runtime/common/dispatcher.h` | common（后端无关） | 声明 `dispatcher_get_callbacks()`，是 common 向分发器"要后端"的唯一入口。 |
| `sw/runtime/common/callbacks.h` | common（后端无关） | 定义 `callbacks_t` 回调表结构与 `vx_dev_init()` 契约——后端要实现什么。 |
| `sw/runtime/common/callbacks.inc` | common（后端无关） | 一段可被 `#include` 的 `vx_dev_init` 模板，免去每个后端手写注册代码。 |
| `sw/runtime/common/device.cpp` | common（后端无关） | `Device::open()` 在这里调用 `dispatcher_get_callbacks`，并据此构造 `CallbacksAdapter`。 |
| `sw/runtime/common/vortex2_internal.h` | common（后端无关） | 定义 `Platform` 抽象基类与 `CallbacksAdapter` 适配器。 |
| `sw/runtime/stub/vortex.cpp` | stub（分发器） | **本讲主角**：`libvortex.so` 的分发器实现，dlopen 后端库。 |
| `sw/runtime/stub/Makefile` | stub（分发器） | 把 common 源码 + 分发器编译成 `libvortex.so`。 |
| `sw/runtime/simx/vortex.cpp` | simx 后端 | SimX 仿真后端，实现 `callbacks_t`，产物 `libvortex-simx.so`。 |
| `sw/runtime/{rtlsim,opae,xrt,gem5}/vortex.cpp` | 其他后端 | 其余四种后端，同样实现 `callbacks_t`。 |

**记忆口诀**：`common/` 不知道后端存在（永远不 `dlopen`、不读环境变量）；`stub/` 只管"选哪个后端并把它装上"；`<name>/` 只管"我是这个后端，这是我的 6 个回调"。三层职责互不越界。

## 4. 核心概念与源码讲解

### 4.1 整体设计：后端无关的 common + 可插拔后端

#### 4.1.1 概念说明

Vortex 的主机运行时面对一个尴尬的现实：同一个 `vx_dev_open` → `vx_mem_alloc` → `vx_start` 的主机程序，既可能在纯软件仿真器（simx、rtlsim、gem5）上跑，也可能在真实的 Intel FPGA（opae）或 Xilinx FPGA（xrt）上跑。这些"后端"的物理机制天差地别——simx 是进程内 C++ 对象，opae 是 PCIe 上的 CCI-P 缓冲区，xrt 是 XRT 的 BO 对象。如果让 `common/` 里的设备管理代码直接 `#ifdef` 区分这些后端，会变成一团乱麻。

Vortex 的解法是经典的"接口与实现分离"：

- 把所有后端**都必须提供**的、最底层的共性能力抽象成一张 C 回调表 `callbacks_t`（只有 6 个函数）。
- `common/` 只依赖这张表（和一个 `dispatcher_get_callbacks` 取表函数），对"后端是谁、怎么加载"一无所知。
- 把"选后端、加载后端"的全部知识（`dlopen`、`getenv("VORTEX_DRIVER")`、库名字符串）集中到 `stub/vortex.cpp` 一个文件里。
- 每个后端各自编译成独立的 `libvortex-<NAME>.so`，实现这张表即可被加载。

这样 `common/` 的一份源码就同时服务所有后端，而新增一个后端（比如接一种新 FPGA 板卡）完全不用动 `common/`。

#### 4.1.2 核心流程

一次 `vx_dev_open` 触发的后端加载流程（伪代码）：

```
主机程序调用 vx_dev_open()
    └─> common/device.cpp: Device::open()
            ├─> 调 dispatcher_get_callbacks(&cb)   // 要一张回调表
            │       │  (stub/vortex.cpp 实现)
            │       ├─ 若后端已加载过：直接返回缓存的 g_backend_cb
            │       ├─ 否则：getenv("VORTEX_DRIVER")，默认 "simx"
            │       ├─ 拼库名 "libvortex-" + drv + ".so"
            │       ├─ dlopen 该库
            │       ├─ dlsym 取出该库的 vx_dev_init 符号
            │       └─ 调 vx_dev_init(&g_backend_cb) 让后端填充回调表
            ├─> cb->dev_open(&dev_ctx)              // 让后端建设备上下文
            └─> new CallbacksAdapter(*cb, dev_ctx)   // 包装成 Platform，供 Device 使用
```

注意一个关键设计：**整个进程只加载一个后端**（`g_backend_lib` 是全局单例），`dispatcher_get_callbacks` 是幂等的——第一次调用真正去 `dlopen`，之后所有 `vx_dev_open` 直接复用同一张表。

#### 4.1.3 源码精读

`stub/vortex.cpp` 顶部的大段注释把这套设计意图讲得很直白。注意它强调的职责边界：common "includes dispatcher.h and asks for a callbacks table; it never touches dlopen, getenv, or the library-name string"——这正是 4.1.1 说的隔离原则。

参见 [sw/runtime/stub/vortex.cpp:L14-L29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L14-L29)，这段注释说明了 `libvortex.so` 的两项职责（聚合 common 入口 + 首次 open 时 dlopen 后端）与 common 必须保持后端无关的纪律。

#### 4.1.4 代码实践

**实践目标**：在动手看分发器细节前，先从工程目录层面确认"一份 common、多个后端"的布局。

**操作步骤**：

1. 列出 `sw/runtime/` 下所有一级子目录：你会看到 `common/ stub/ simx/ rtlsim/ opae/ xrt/ gem5/`。
2. 打开 `sw/runtime/Makefile`，看它的 `all` 目标依赖哪些子目录。
3. 打开 `sw/runtime/stub/Makefile` 的 `SRCS` 列表，确认 common 的设备/缓冲/队列等源码被编译进了 `libvortex.so`。

**需要观察的现象**：`stub/Makefile` 的 `SRCS` 里包含 `device.cpp buffer.cpp queue.cpp event.cpp module.cpp vm.cpp ...` 等 common 文件，但**不包含**任何 `simx/opae/xrt` 的源码；后者的源码各自在 `simx/Makefile` 等里编译成 `libvortex-<NAME>.so`。

**预期结果**：你会看到 `libvortex.so`（含 common + 分发器）和若干 `libvortex-<NAME>.so`（后端）是**分开编译、分开产出**的两个家族，运行时再由 dlopen 拼接。参见 [sw/runtime/stub/Makefile:L38-L52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/Makefile#L38-L52)。

#### 4.1.5 小练习与答案

**练习 1**：如果要在 Vortex 里新增一种 FPGA 后端（比如某种自研加速卡），需要修改 `common/` 下的代码吗？

**参考答案**：不需要。只需在 `sw/runtime/` 下新建一个目录，写一个实现 `callbacks_t` 那 6 个函数的 `vortex.cpp`（用 `#include <callbacks.inc>` 注册），加一个产出 `libvortex-<NAME>.so` 的 `Makefile`，然后在 `sw/runtime/Makefile` 的 `all` 里挂上即可。`common/` 与 `stub/` 都不用动——这正是接口与实现分离带来的可扩展性。

**练习 2**：为什么分发器要用"进程级单例后端"（`g_backend_lib` 全局只存一个），而不是每次 `vx_dev_open` 都允许换后端？

**参考答案**：因为一个进程内的所有 `vx::Device` 都共享同一套 CP 命令协议和同一份 common 状态，混用多个物理后端没有意义且会引发混乱；单例化让 dlopen 只发生一次、`vx_dev_init` 只跑一次，避免重复加载与重复初始化的开销。注意 `Device::open` 里还有 `if (index != 0) return VX_ERR_INVALID_VALUE;`——"one device per backend"，每后端只支持 0 号设备。

---

### 4.2 dispatcher.h 契约：common 如何"要"一个后端

#### 4.2.1 概念说明

`dispatcher.h` 是 `common/` 与 `stub/` 之间的窄接口。它只声明了一个函数 `dispatcher_get_callbacks`：调用方（common）传入一个二级指针，函数把"已经填好的回调表地址"写进去。这个头文件故意写得很小，且**只 include `callbacks.h` 和 `<vortex.h>`**——它刻意不 include `<dlfcn.h>`，让 common 编译时根本看不到 `dlopen` 的存在，从而物理上阻止 common 去碰后端加载逻辑。

#### 4.2.2 核心流程

`dispatcher_get_callbacks` 对调用方的语义契约（由注释固定）：

1. 加载（或复用）`$VORTEX_DRIVER`（默认 `simx`）指定的后端库。
2. 成功时通过 `out` 返回指向回调表的指针。
3. 该指针由分发器拥有，**进程生命周期内一直有效**。
4. 幂等：重复调用返回同一张表，不会重新加载。

#### 4.2.3 源码精读

整个头文件就一个声明，但注释里把契约写得很完整：

[sw/runtime/common/dispatcher.h:L17-L27](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/dispatcher.h#L17-L27) —— 注意第 17 行 `#include "callbacks.h"` 和第 18 行 `#include <vortex.h>` 是它仅有的依赖；第 27 行就是那个唯一函数声明，注释强调 "common/ stays backend-agnostic — it must not know about dlopen or VORTEX_DRIVER"。

common 侧的取用点在 `Device::open`：

[sw/runtime/common/device.cpp:L137-L158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L137-L158) —— 第 142 行 `dispatcher_get_callbacks(&cb)` 拿到表；第 146 行 `cb->dev_open(&dev_ctx)` 让后端建立自己的设备上下文；第 149 行 `new CallbacksAdapter(*cb, dev_ctx)` 把这张 C 回调表包装成 C++ 的 `Platform` 对象交给 `Device`。

#### 4.2.4 代码实践

**实践目标**：确认 common 真的"看不到"后端加载细节。

**操作步骤**：

1. 在 `sw/runtime/common/` 下搜索 `dlopen`、`VORTEX_DRIVER`、`dlfcn.h` 这几个关键词。

**需要观察的现象**：在 `common/` 下应当**搜不到** `dlopen` 与 `getenv("VORTEX_DRIVER")`；这些字符串只出现在 `stub/vortex.cpp` 里。

**预期结果**：印证"`common/` 后端无关"不是一句口号，而是由头文件依赖（dispatcher.h 不 include dlfcn.h）和符号分布共同强制保证的工程约束。

#### 4.2.5 小练习与答案

**练习 1**：`dispatcher_get_callbacks` 为什么返回"指向回调表的指针（二级指针出参）"而不是直接返回 `callbacks_t` 结构体值？

**参考答案**：因为回调表里存的是函数指针，由分发器的全局变量 `g_backend_cb` 持有；返回指向它的指针可以避免拷贝整张表、保证所有调用者看到同一份表（指针"进程生命周期内有效"），也让分发器能在首次调用时原地填充它。

**练习 2**：如果 `dispatcher_get_callbacks` 返回失败（非 `VX_SUCCESS`），`Device::open` 会怎样？

**参考答案**：第 143 行 `if (r != VX_SUCCESS) return r;` 直接把错误码向上返回，不会继续去 `dev_open` 或构造 `CallbacksAdapter`，调用方拿到的就是 `vx_dev_open` 失败。

---

### 4.3 stub/vortex.cpp：dlopen 后端分发器精读

#### 4.3.1 概念说明

这是本讲主角。`stub/vortex.cpp` 编译进 `libvortex.so`，是主机程序真正 link 的那个库。它实现了 `dispatcher_get_callbacks`，做四件事：读环境变量决定后端名、拼库名、`dlopen` 加载、`dlsym` 找到后端的 `vx_dev_init` 并调用它填充回调表。整个过程是惰性的——只在真正需要时（第一次 `Device::open`）才执行。

#### 4.3.2 核心流程

```
dispatcher_get_callbacks(out):
  if out == nullptr: return VX_ERR_INVALID_VALUE
  if g_backend_lib != nullptr:        // 已加载
      *out = &g_backend_cb;  return VX_SUCCESS
  drv = getenv("VORTEX_DRIVER")
  if drv == nullptr: drv = "simx"     // 默认后端
  lib = "libvortex-" + drv + ".so"
  h = dlopen(lib, RTLD_LAZY)
  if h == nullptr: 报错; return VX_ERR_DEVICE_LOST
  init = dlsym(h, "vx_dev_init")      // 后端必须导出这个符号
  if init == nullptr: dlclose(h); return VX_ERR_DEVICE_LOST
  if init(&g_backend_cb) != 0: dlclose(h); return VX_ERR_DEVICE_LOST
  g_backend_lib = h                   // 缓存，此后幂等
  *out = &g_backend_cb
  return VX_SUCCESS
```

这里有两个工程要点：

- **惰性 + 缓存**：`g_backend_lib` 一旦非空就短路返回，保证整进程只 `dlopen` 一次。`RTLD_LAZY` 让符号在首次用到时才解析，加快启动。
- **契约靠符号名绑定**：分发器不认识任何后端类型，它只认 `vx_dev_init` 这个导出符号——谁提供了这个符号、能正确填充 `callbacks_t`，谁就是合法后端。

#### 4.3.3 源码精读

先看两个进程级全局变量，它们是"单例后端"的载体：

[sw/runtime/stub/vortex.cpp:L42-L43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L42-L43) —— `g_backend_lib` 保存 dlopen 句柄，`g_backend_cb` 保存被后端填好的回调表；注释点明"One backend per process; reused across vx_device_open calls"。

接着看核心函数的四个步骤：

[sw/runtime/stub/vortex.cpp:L57-L61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L57-L61) —— 第 57-58 行读环境变量并默认 `simx`；第 59 行拼库名 `libvortex-<drv>.so`；第 61 行 `dlopen`。这就是 `$VORTEX_DRIVER` 决定加载哪个 `.so` 的全部逻辑。

[sw/runtime/stub/vortex.cpp:L68-L75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L68-L75) —— 用 `dlsym` 从后端库里取出名为 `vx_dev_init` 的函数（类型是 `int (*)(callbacks_t*)`），取不到就 `dlclose` 回收并报错。

[sw/runtime/stub/vortex.cpp:L77-L86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L77-L86) —— 调用后端的 `vx_dev_init(&g_backend_cb)` 让它填充回调表；成功后第 84 行缓存句柄、第 85 行把表地址写给调用方。

那么"运行时去哪里找这些 `libvortex-<NAME>.so`"？答案是链接时的 rpath：

[sw/runtime/stub/Makefile:L17-L20](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/Makefile#L17-L20) —— `-Wl,-soname,libvortex.so` 给主库起名，`-Wl,-rpath,'$ORIGIN'` 让 `libvortex.so` 运行时到"自己所在目录"去找兄弟库 `libvortex-<NAME>.so`；所以测试脚本只要把 `LD_LIBRARY_PATH` 指向产出目录即可。

实际测试套件就是这么用的，例如 `tests/regression/common.mk` 里跑 simx 的规则形如：

```
LD_LIBRARY_PATH=$(VORTEX_RT_LIB):$(LD_LIBRARY_PATH) VORTEX_DRIVER=simx ./$(PROJECT) $(OPTS)
```

把 `VORTEX_DRIVER` 设成 `rtlsim/opae/xrt` 就切换到别的后端（opae/xrt 还要额外设 `SCOPE_JSON_PATH`、`XRT_XCLBIN_PATH` 等）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手验证 `$VORTEX_DRIVER` 如何决定加载哪个 `.so`，并理解 `vx_dev_init` 是后端唯一被分发器调用的符号。

**操作步骤**：

1. 确认 `stub/vortex.cpp` 第 59 行的库名拼接规则：`libvortex-` + `$VORTEX_DRIVER` + `.so`。
2. 在某个后端（如 simx）的 `vortex.cpp` 文件末尾找到 `#include <callbacks.inc>`，确认它通过这个模板导出了 `vx_dev_init`。参见 [sw/runtime/simx/vortex.cpp:L172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L172)。
3. 找一个已构建好的 build 目录（若已执行过 u1-l4 的 blackbox），用 `ls` 查看 runtime 产出，应能看到 `libvortex.so` 和 `libvortex-simx.so` 并列。
4. 设 `VORTEX_DRIVER=simx` 跑一次 demo；再故意设一个不存在的值，如 `VORTEX_DRIVER=nosuch`，跑同一个 demo。

**需要观察的现象**：

- 正常情况：程序打印 `PASSED!`，退出码 0。
- `VORTEX_DRIVER=nosuch`：分发器在 `dlopen("libvortex-nosuch.so")` 失败，stderr 打印形如 `vortex: cannot open backend library 'libvortex-nosuch.so': ...`，并返回 `VX_ERR_DEVICE_LOST`，`vx_dev_open` 失败。

**预期结果**：这正面证明"后端选择完全由环境变量 + dlopen 完成"，且错误信息直接来自 [sw/runtime/stub/vortex.cpp:L62-L66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L62-L66) 这段。若本地尚未构建，输出"待本地验证"，但错误路径可由源码直接推断。

**列出 simx 后端需实现的关键回调**：simx 后端（`sw/runtime/simx/vortex.cpp`）通过 `callbacks.inc` 注册的 6 个回调对应它的 6 个成员方法：

| `callbacks_t` 字段 | simx 实现 | 作用 |
| --- | --- | --- |
| `dev_open` | 构造 `vx_device` 并调 `init()` | 建立 SimX 仿真设备上下文 |
| `dev_close` | `delete` 设备对象 | 释放仿真器 |
| `cp_reg_write` | `cp_.mmio_write` + 推进 256 tick | 向 CP 模型写寄存器（命令环形 doorbell） |
| `cp_reg_read` | 推进 tick 后 `cp_.mmio_read` | 从 CP 模型读寄存器（如完成状态轮询） |
| `host_mem_alloc` | `aligned_alloc` 一块进程内存 | 分配 CP 可 DMA 的主机暂存（统一内存，cp_addr 即指针值） |
| `host_mem_free` | `free` 该内存 | 释放上述暂存 |

#### 4.3.5 小练习与答案

**练习 1**：为什么分发器用 `dlsym(h, "vx_dev_init")` 而不是直接 `dlsym` 每一个回调函数（如 `dlsym(h, "cp_reg_write")`）？

**参考答案**：因为回调数量多、且每个后端的设备上下文需要一次性建立。让后端导出唯一的 `vx_dev_init(callbacks_t*)`，由它自己把 6 个函数指针（在 `callbacks.inc` 里是 6 个 lambda）填进表里，比分发器逐个 `dlsym` 更内聚——后端的内部类名、上下文结构对分发器完全透明。

**练习 2**：如果把 `VORTEX_DRIVER` 设为空字符串（`VORTEX_DRIVER=`），会发生什么？

**参考答案**：`getenv` 返回非空指针（指向空串），分发器不会回退到默认 `simx`，而是拼出 `libvortex-.so` 去 dlopen，必然失败并打印 `cannot open backend library 'libvortex-.so'`，返回 `VX_ERR_DEVICE_LOST`。只有当 `getenv` 返回 `nullptr`（变量根本未设）时才默认 `simx`（见第 57-58 行）。

---

### 4.4 callbacks.h 契约与 callbacks.inc 模板：后端必须实现什么

#### 4.4.1 概念说明

`callbacks.h` 把"后端要实现什么"收缩到极致：**只有三类、共 6 个能力**。注释明确把后端定义为一个"pure transport HAL"（纯传输硬件抽象层）——它只管把字节搬到 CP 能看见的地方，至于设备内存怎么分配、DMA 怎么编排、能力码怎么解码，统统住在 common 里。这种"瘦后端、胖 common"的设计是 Vortex 能用一份 common 服务五种天差地别后端的关键。

三类能力是：

1. **设备生命周期**：`dev_open`（建上下文）、`dev_close`（销毁）。
2. **CP 寄存器通道**：`cp_reg_write` / `cp_reg_read`——对 CP 寄存器堆的 32 位读写窗口，是整条控制面（doorbell、完成轮询、能力查询）的唯一入口。
3. **CP 可见的主机内存**：`host_mem_alloc` / `host_mem_free`——分配主机侧内存（命令环 + DMA 暂存区），同时返回 CPU 指针与 CP 侧地址。

#### 4.4.2 核心流程

`callbacks.inc` 是一段"模板"：它实现了 `vx_dev_init(callbacks_t*)`，把 6 个 lambda 填进表里，每个 lambda 内部转调后端 `vx_device` 类的对应方法。所以后端作者只需：

```
1. 写一个 vx_device 类，提供 6 个方法：
     int init();
     int cp_reg_write(uint32_t off, uint32_t value);
     int cp_reg_read (uint32_t off, uint32_t* value);
     int host_mem_alloc(uint64_t size, void** host_ptr, uint64_t* cp_addr);
     int host_mem_free (uint64_t cp_addr);
   (dev_open/dev_close 由模板负责 new/delete)
2. 在文件末尾 #include <callbacks.inc>
```

模板里的 `dev_open` lambda 负责 `new vx_device()` → `init()` → 把对象指针作为不透明 `void*` 上下文交出去；其余 lambda 每次都把 `void*` 转回 `vx_device*` 再调用方法。这就是 C ABI 与 C++ 对象之间的桥。

#### 4.4.3 源码精读

先看契约里"三类能力"的权威表述：

[sw/runtime/common/callbacks.h:L21-L26](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.h#L21-L26) —— 点明后端是 pure transport HAL，只提供 device lifecycle / register channel / CP-visible host memory 三件东西，全部返回值"0 成功、非 0 失败"。

完整的 `callbacks_t` 结构：

[sw/runtime/common/callbacks.h:L39-L68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.h#L39-L68) —— 注意每个字段的注释都强调"off 是 CP 内部寄存器偏移"、"host_mem_alloc 同时返回 CPU 指针与 CP 侧地址"等约定；第 72 行声明 `vx_dev_init`，注释点明"每个后端用 `<callbacks.inc>` 模板来实现它"。

再看模板如何把 C++ 方法接到 C 表上：

[sw/runtime/common/callbacks.inc:L32-L58](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.inc#L32-L58) —— `vx_dev_init` 用 `new vx_device()` 建对象、调 `init()`、把对象指针作为 `out_dev_ctx` 交出（第 37-50 行的 `dev_open` lambda）；`dev_close` lambda 则 `delete` 它（第 52-58 行）。

[sw/runtime/common/callbacks.inc:L60-L90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.inc#L60-L90) —— 其余 4 个 lambda 把 `void* dev_ctx` 转回 `vx_device*`，转调对应方法；每个 lambda 都先判空保护，体现 HAL 边界的健壮性。

common 侧用 `CallbacksAdapter` 把这张 C 表再包回 C++ 虚接口 `Platform`，供 `Device` 使用：

[sw/runtime/common/vortex2_internal.h:L119-L138](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L119-L138) —— `Platform` 抽象基类定义了 common 想要的虚接口。

[sw/runtime/common/vortex2_internal.h:L151-L183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L151-L183) —— `CallbacksAdapter` 持有 `callbacks_t` 表与设备上下文，每个虚函数都转调表里对应函数指针（第 160 行的 `r()` 把"0/非0"压回 `vx_result_t`）；析构时自动调 `cb_.dev_close`。

至此整条适配链闭合：`Device`（C++）→ `Platform` 虚接口（C++）→ `CallbacksAdapter`（C++→C 桥）→ `callbacks_t` 函数指针（C ABI）→ 后端 `vx_device` 方法。所有后端差异被压缩在那张表里。

#### 4.4.4 代码实践

**实践目标**：以 simx 后端为例，看清 6 个回调与后端类的对应关系。

**操作步骤**：

1. 打开 `sw/runtime/simx/vortex.cpp`，定位 `class vx_device` 的 6 个方法。

**需要观察的现象与预期结果**：

- `init()`（[sw/runtime/simx/vortex.cpp:L64-L68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L64-L68)）：直接返回 0，因为 VM 全住在 common 里，后端无需做平台初始化（这与 opae/xrt 形成对比，它们要在 `init()` 里枚举并打开 FPGA）。
- `cp_reg_write` / `cp_reg_read`（[sw/runtime/simx/vortex.cpp:L74-L83](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L74-L83)）：调一个软件 CP 模型（`cp_.mmio_write/mmio_read`），并在每次 MMIO 前后推进最多 256 个 tick 让 CP 保持响应——因为没有独立仿真线程。
- `host_mem_alloc` / `host_mem_free`（[sw/runtime/simx/vortex.cpp:L88-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L88-L110)）：因为是统一内存、仿真在进程内，所以 `cp_addr` 就直接是 `aligned_alloc` 返回的指针值本身（第 96 行 `*cp_addr = reinterpret_cast<uint64_t>(ptr)`）。
- CP 的 dram hooks（[sw/runtime/simx/vortex.cpp:L128-L162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L128-L162)）：`make_cp_hooks()` 用 lambda 把 dram 读写接到 RAM 或主机暂存区，`vortex_start` 用 `std::async` 起后台线程跑 `processor_.run()`。

你会看到 simx 后端**整个文件没有一行设备内存分配逻辑**——印证"瘦后端"原则。

#### 4.4.5 小练习与答案

**练习 1**：`CallbacksAdapter::r(int rc)` 把后端返回的"0 成功/非 0 失败"压成 `vx_result_t`，但它把所有非 0 值都映射成 `VX_ERR_INVALID_VALUE`（参见 [vortex2_internal.h:L160-L162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L160-L162)）。这会带来什么后果？

**参考答案**：后端具体的错误码（比如 FPGA 上的 MMIO 超时码、XRT 异常类型）在跨过 HAL 边界时被"抹平"成一个通用的失败码，调用方只知道"失败了"，不知道"为什么失败"。这是为了保持 C ABI 简单（callbacks_t 约定全部用 0/非 0）付出的诊断信息代价，和 u3-l1 里同步封装 `to_int` 丢失具体错误码是同一种取舍。

**练习 2**：`host_mem_alloc` 为什么要同时返回 `out_host_ptr`（CPU 指针）和 `out_cp_addr`（CP 侧地址）两个值？能不能只返回一个？

**参考答案**：不能。因为主机用 CPU 指针往里 `memcpy` 命令环/数据，而 CP 用设备侧地址去 DMA 同一块字节；在很多后端这两个地址并不相等（opae 里 CPU 指针是 host VA、CP 地址是 IO 地址；gem5 里 CPU 指针是 `PIN_BASE_ADDR+addr`、CP 地址是裸 VRAM 偏移）。只有 simx/rtlsim 这种进程内统一内存里两者恰好相等。所以契约必须同时返回两个。

---

### 4.5 五种驱动后端的定位差异

#### 4.5.1 概念说明

同一个 `callbacks_t` 契约，五种后端的实现反映了它们物理载体的差异。理解这五种差异，就理解了"为什么要把后端抽象成 HAL"。

| 后端 | 产物 | 载体 | `dev_open` 干什么 | `host_mem_alloc` 用什么 | 典型用途 |
| --- | --- | --- | --- | --- | --- |
| **simx** | `libvortex-simx.so` | 进程内 C++ 仿真器（SimX） | 构造 `RAM`+`Processor`+CP 模型 | 进程内 `aligned_alloc`（统一内存，cp_addr=指针） | 软件开发、功能验证、SimX↔RTL lockstep 的 oracle（见 u7-l4） |
| **rtlsim** | `libvortex-rtlsim.so` | Verilator RTL 仿真（进程内） | 构造 `RAM`+`Processor`（Verilator）+CP 模型 | 同 simx，进程内分配 | RTL 功能/时序验证、model_parity 的另一半 |
| **opae** | `libvortex-opae.so` | Intel FPGA（OPAE/CCI-P） | 按 UUID 枚举并打开加速器 | CCI-P 共享缓冲 `fpgaPrepareBuffer`（host VA + IO 地址） | Intel FPGA 上跑真实 Vortex |
| **xrt** | `libvortex-xrt.so` | Xilinx FPGA（XRT） | 打开设备、加载 `xclbin`、打开 kernel IP | XRT host-only BO（`xrt::bo`，硬件）或进程内存（`xrtsim`） | Xilinx FPGA / XRT 仿真 |
| **gem5** | `libvortex-gem5-x86_64.so` 等 | gem5 全系统模拟器 | `drv_init` 连接 gem5 设备 | 从 PIN 窗口顶部的 VRAM 孔径切出 | gem5 全系统研究 |

#### 4.5.2 核心流程：CP 寄存器偏移如何落地

一个最能体现差异的细节是：common 传给 `cp_reg_write(off, ...)` 的 `off` 永远是 **CP 内部寄存器偏移**，但各后端要把它翻译成各自的物理地址：

```
simx / rtlsim : 直接喂给进程内 CP 模型的 mmio_write(off)        （无需加基址）
opae          : 写 fpgaWriteMMIO64(0x1000 + off)                 （AFU 把 0x1000..0x1FFF 解复用到 CP 寄存器堆）
xrt           : write_register(0x1000 + off)                     （AXI-Lite 解复用同理）
gem5          : PIO 写 PIO_BASE_ADDR + off                       （gem5 设备的 PIO 范围本身就是 CP 寄存器堆，无 0x1000 偏移）
```

simx/rtlsim 还有一个共同套路：因为它们用"功能性 CP C++ 模型"而非硬件 CP，所以每次 MMIO 前后要手动推进最多 256 个 tick，否则 CP 模型不会自动前进。这是仿真后端特有的"手动泵"。

#### 4.5.3 源码精读

**simx vs rtlsim（仿真兄弟）**：两者几乎一模一样，都用功能性 CP 模型 + 进程内统一内存。差别仅在 `ram_` 构造的页大小（simx 用 `VX_VM_PAGE_SIZE`，rtlsim 用 `RAM_PAGE_SIZE`）与背后 `Processor` 是 SimX 还是 Verilator。参见 [sw/runtime/simx/vortex.cpp:L45-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L45-L53) 与 [sw/runtime/rtlsim/vortex.cpp:L45-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/rtlsim/vortex.cpp#L45-L53)。两者的注释都强调 "rtlsim/simx has unified memory... the sim runs in-process"。

**opae（Intel FPGA）**：`init()` 要做大量平台工作——枚举加速器、按 UUID 打开。参见 [sw/runtime/opae/vortex.cpp:L85-L164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L85-L164)；寄存器通道加上 AFU 基址 0x1000，见 [sw/runtime/opae/vortex.cpp:L166-L184](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L166-L184)；主机内存用 CCI-P 共享缓冲，CPU 指针与 IO 地址分离，见 [sw/runtime/opae/vortex.cpp:L189-L204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L189-L204)。

**xrt（Xilinx FPGA）**：与 opae 同构但用 XRT API，且要小心 C++ 异常不能穿过 `extern "C"` 边界——每个回调都用 `XRT_TRY/XRT_CATCH` 把异常吞掉转成返回码，见 [sw/runtime/xrt/vortex.cpp:L97-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L97-L110) 的注释与宏；寄存器通道同样加 0x1000 基址，见 [sw/runtime/xrt/vortex.cpp:L72-L76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L72-L76)。

**gem5（全系统模拟器）**：最特殊。注释点明它的架构独特性——CP 跑在设备 SimObject 里、主机运行时跑在被模拟的 CPU 上，两个域唯一共同可达的内存是设备 VRAM，所以"CP 可见主机内存"必须是 VRAM。见 [sw/runtime/gem5/vortex.cpp:L14-L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/vortex.cpp#L14-L36)；它的寄存器通道用 32 位 PIO 且**不加** 0x1000 偏移（因为 gem5 设备的 PIO 范围本身就是 CP 寄存器堆），见 [sw/runtime/gem5/vortex.cpp:L75-L84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/vortex.cpp#L75-L84)；主机内存从 PIN 窗口顶部的 VRAM 孔径切出，CPU 指针=`PIN_BASE_ADDR+addr`、CP 地址=裸 `addr`，见 [sw/runtime/gem5/vortex.cpp:L89-L97](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/vortex.cpp#L89-L97) 与孔径常量 [L49-L50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/vortex.cpp#L49-L50)。

> **关于 gem5 库名的说明（待本地验证）**：分发器第 59 行拼出的库名是 `libvortex-<drv>.so`（即 `libvortex-gem5.so`），但 gem5 的 Makefile 产出的是带架构后缀的 `libvortex-gem5-$(ARCH_SUFFIX).so`（如 `libvortex-gem5-x86_64.so`，见 [sw/runtime/gem5/Makefile:L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/Makefile#L53)）。这两者并不直接匹配，因此 gem5 后端的实际加载方式（可能依赖 symlink、改名的库，或 gem5 独有的装载路径）需在本地构建 gem5 后再确认；这是 gem5"架构独特性"在分发层的又一体现。

#### 4.5.4 代码实践

**实践目标**：通过对比"主机内存"这一项，直观感受同一契约下的物理差异。

**操作步骤**：

1. 对照下表，分别打开四个后端的 `host_mem_alloc`，看 `*cp_addr` 被赋成什么：
   - simx：[L96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L96) —— `cp_addr = 指针`
   - opae：[L202](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L202) —— `cp_addr = ioaddr`（与 host VA 不同）
   - xrt：[L278](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L278) —— `cp_addr = bo.address()`（kernel 可见地址）
   - gem5：[L94-L95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/gem5/vortex.cpp#L89-L97) —— `host_ptr = PIN_BASE_ADDR+addr`，`cp_addr = addr`

**需要观察的现象**：同样是"分配一块 CP 能 DMA 到的主机内存"，四个后端返回的 `cp_addr` 语义完全不同——这正是 4.4.5 练习 2 里"必须同时返回两个地址"的现实原因。

**预期结果**：你会更深刻地理解，`common/` 之所以敢对后端一无所知，正是因为这些差异被 `callbacks_t` 的 6 个函数"吸收"了。

#### 4.5.5 小练习与答案

**练习 1**：simx 和 rtlsim 的 `vortex.cpp` 几乎逐行相同，为什么不抽成一个共享文件？

**参考答案**：因为它们背后 link 的 `Processor`/RAM 实现不同（SimX 仿真库 vs Verilator 编译出的库），构造参数、tick 行为也有细微差别（如 `ram_` 页大小常量不同）。当前以"复制+小改"换取各自独立编译、独立依赖（simx 链 `libsimx.so`，rtlsim 链 Verilator 库）的清晰边界。这是一个工程取舍，理论上也可以用模板/继承合并，但收益有限。

**练习 2**：opae 与 xrt 的 `init()` 都在 `dev_open` 时（经 `callbacks.inc` 模板）做了大量 FPGA 平台初始化，而 simx 的 `init()` 直接返回 0。这暗示了什么？

**参考答案**：暗示"设备发现与打开"是真实硬件后端独有的负担（枚举板卡、加载固件/`xclbin`、打开 kernel IP），而纯软件仿真后端没有这些物理步骤。把这些差异全部塞进 `init()`、对 common 只暴露统一的"成功/失败"，正是 HAL 的价值所在。

## 5. 综合实践

**任务**：从一次 `vx_dev_open` 出发，完整追踪"主机程序 → 后端选择 → 后端建立 → common 接管"的链路，并画出对象关系图。

**步骤**：

1. 选一个已构建的 build 目录（参考 u1-l4）。用 `ldd` 查看某个测试程序（如 `tests/regression/demo` 产物）依赖的 `libvortex.so`，确认它**不**直接依赖任何 `libvortex-<NAME>.so`（后者是运行时 dlopen 的，不在 `ldd` 静态依赖里）。
2. 设 `VORTEX_DRIVER=simx` 跑 demo；用 `strace -f -e openat ./demo ... 2>&1 | grep libvortex`（若环境允许）观察进程在首次 `vx_dev_open` 时打开了哪个 `.so`。预期看到 `libvortex-simx.so` 被打开，且仅打开一次。
3. 对照本讲 4.1.2 的伪代码流程，按顺序在以下源码点之间画箭头：
   - `vx_dev_open`（u3-l1 的同步封装）
   - `Device::open`（[device.cpp:L137-L158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L137-L158)）
   - `dispatcher_get_callbacks`（[stub/vortex.cpp:L49-L87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/stub/vortex.cpp#L49-L87)）
   - 后端 `vx_dev_init`（经 [callbacks.inc:L32-L93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.inc#L32-L93)）
   - 后端 `dev_open` lambda → `new vx_device()` + `init()`
   - 回到 common：`new CallbacksAdapter(*cb, dev_ctx)`（[vortex2_internal.h:L151-L183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L151-L183)）
4. 在对象关系图上标注：`Device` 持有 `Platform*`（实际是 `CallbacksAdapter`），后者持有 `callbacks_t` 表与后端 `dev_ctx`（实际是后端 `vx_device*`）。之后所有 CP 寄存器读写、主机内存分配都沿这条链下沉到后端。

**预期结果**：你会得到一张清晰的"主机调用 → C++ 虚接口 → C 回调表 → 后端实现"的对象/调用关系图，彻底打通 u3-l1 到本讲的主机侧运行时全景。若 strace/ldd 因环境受限无法运行，相关观察标注"待本地验证"，但调用链可完全由源码确定。

## 6. 本讲小结

- Vortex 主机运行时用"接口与实现分离"让一份 `common/` 源码服务五种后端：`common/` 后端无关，`stub/` 专门负责 dlopen 选后端，`<name>/` 各自实现后端。
- 后端选择完全在运行时完成：首次 `Device::open` 时 `stub/vortex.cpp` 读 `$VORTEX_DRIVER`（默认 `simx`），拼出 `libvortex-<NAME>.so` 并 `dlopen`，再用 `dlsym` 取后端的 `vx_dev_init` 让其填充 `callbacks_t`。整个进程只加载一个后端（幂等单例）。
- `dispatcher.h` 是 common 与 stub 之间的窄接口，刻意不让 common 碰 `dlopen`/环境变量，从依赖关系上强制隔离。
- `callbacks_t` 把每个后端收缩成"纯传输 HAL"——只暴露设备生命周期、CP 寄存器通道、CP 可见主机内存三类共 6 个 C 函数指针；`callbacks.inc` 模板负责把这 6 个指针接到后端 `vx_device` 类的方法上。
- common 侧用 `CallbacksAdapter` 把 C 回调表再包成 C++ `Platform` 虚接口，供 `Device` 使用，完成 C ABI 与 C++ 对象之间的双向桥接。
- 五种后端（simx/rtlsim/opae/xrt/gem5）在同一个契约下各显其物理特性：仿真后端用进程内统一内存与功能性 CP 模型（cp_addr=指针、需手动 tick）；FPGA 后端用 CCI-P/BO 缓冲（CPU 指针与设备地址分离）并在 `init()` 里做平台发现；gem5 最特殊，主机内存就是 VRAM。

## 7. 下一步学习建议

- 本讲只讲"后端如何被选中与加载"，但"选中之后一次 `vx_start` 怎么把命令送进 CP"留给了 **u3-l4（主机→设备启动流程与 .vxbin 加载）**——那里会展开 `module.cpp` 解析 `.vxbin` 与命令进 CP 环的细节。
- CP 这个反复出现的"命令处理器"是后端唯一暴露的控制面，它的硬件实现详见 **u11-l3（命令处理器与 KMU）**，会讲 `VX_cp_core.sv` / `VX_cp_launch.sv` 如何解包并派发 CTA。
- 想看 SimX 后端内部那个 `Processor`/CP 模型到底怎么跑起来，进入 **u5（SimX 模拟器框架）** 与 **u5-l2（SimX 入口与处理器层次）**，那里讲 `sim/simx/main.cpp` 的启动序列。
- 如果你的兴趣在 FPGA 落地，**u14-l1（FPGA AFU 外壳与驱动）** 会把 opae/xrt 这两条 PCIe 路径与 AFU 外壳讲透。
