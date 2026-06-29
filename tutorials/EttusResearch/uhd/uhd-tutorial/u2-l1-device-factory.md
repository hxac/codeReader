# 设备发现与工厂模式

## 1. 本讲目标

本讲进入 UHD 的核心驱动主线。UHD 要支持从最早的 USRP1 到最新的 X410 等十几种完全不同的硬件，它们的传输方式（USB、以太网、PCIe）、寄存器映射、FPGA 架构都各不相同，但上层用户却只需要一套统一的 `multi_usrp` API。这之所以可能，靠的就是本讲要讲的「设备工厂」机制。

学完本讲你应该能够：

- 说清 `uhd::device` 这个抽象类的职责，以及它对外暴露的 `register_device` / `find` / `make` 三件套分别做什么。
- 理解「设备自注册」：为什么用户代码里没有任何 `if (设备类型 == B200)` 的分支，UHD 却能找到并构造正确的设备对象。
- 读懂 `device.cpp` 里发现（find）与制造（make）的完整链路：并行探测、按 hint 过滤、哈希去重、设备复用。
- 在源码中定位所有调用 `register_device` 的设备实现文件，并说出它们各对应哪一类硬件。

本讲承接 [u1-l6](u1-l6-first-example-rx-to-file.md)：那一讲的 `multi_usrp::make` 内部最终会落到本讲的 `device::make`。理解了工厂机制，你就拿到了打开 UHD 整个设备层的钥匙。

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉。

**比喻一：招聘会（自注册）。** 想象 UHD 是一场招聘会大厅。大厅本身不认识任何一家公司，但每家公司（每个设备实现文件）都自带一名前台，在大厅开门（程序启动）前就主动把自己的「招聘流程」交给了大厅总台。大厅不需要维护一张硬编码的公司清单——谁来了谁登记。这就是「自注册」（self-registration）：新增一种设备只要新写一个实现文件并被链接进来，它就会自动把自己登记进系统，无需修改任何中央清单。

**比喻二：工厂方法（find / make）。** UHD 把「设备」这件事拆成两步：

- **find（发现）**：只问「系统里有哪些设备、它们各自的关键参数（序列号、类型、地址）是什么」，不真正打开设备。这像招聘会里发简历——轻量、可以反复调用。
- **make（制造）**：根据 find 拿到的地址，真正打开设备、分配资源、返回一个可用的设备对象。这像正式入职——重，且对同一台设备通常只做一次。

**比喻三：filter（过滤器）。** USRP 是射频收发设备，但 Ettus 还有一种叫 OctoClock 的「时钟分配设备」。`find`/`make` 提供一个过滤器参数，让你只找射频设备、只找时钟设备，或全都要。

下面几个术语在本讲会反复出现，先记一下：

- `device_addr_t`：一个键值对容器（类似 `map<string,string>`），用来描述一台设备的「地址/提示」。下一讲 [u2-l2](u2-l2-device-addr.md) 会专门讲它，本讲只需要把它理解成「一张写着 type、serial、addr 等字段的卡片」。
- `device_addrs_t`：就是 `std::vector<device_addr_t>`，一组地址。
- `sptr`：`std::shared_ptr<device>` 的别名，UHD 用智能指针管理设备对象的生命周期。
- hint：调用 `find`/`make` 时传入的「提示地址」，可以是空的（找所有设备），也可以填几个关键字段（只找符合条件的那台）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `host/include/uhd/device.hpp` | 设备抽象类的公共头文件：声明 `find`/`make`/`register_device` 三件套与 `device_filter_t` 枚举。 |
| `host/lib/device.cpp` | 三件套的实现：注册表容器、`find` 的并行发现+过滤+去重、`make` 的制造+设备复用。本讲的主角。 |
| `host/include/uhd/utils/static.hpp` | 自注册所用的两个宏 `UHD_STATIC_BLOCK` 与 `UHD_SINGLETON_FCN`。 |
| `host/lib/utils/static.cpp` | `_uhd_static_fixture` 构造函数的实现，揭示「为什么注册会自动发生」。 |
| `host/lib/usrp/*/*_impl.cpp` 等 | 各硬件实现文件，每个文件末尾都有一个 `UHD_STATIC_BLOCK` 调用 `register_device` 把自己登记进系统。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看抽象类（`device.hpp`），再看注册机制（`register_device` + 自注册），然后是 `find`，最后是 `make`。

### 4.1 device 抽象类：三件套的接口（device.hpp）

#### 4.1.1 概念说明

`uhd::device` 是所有具体设备（B200、X300、MPMD 设备等）的抽象基类。它本身不知道任何硬件细节，只定义了三件事：

1. **怎么发现设备**（`find`）；
2. **怎么制造设备**（`make`）；
3. **设备类应该提供哪些通用能力**（如 `get_rx_stream`、`get_tree`）。

其中第 1、2 点就是工厂模式（Factory Pattern）：用一个统一的静态接口，根据传入的地址，返回不同的具体子类对象。上层代码（`multi_usrp`、命令行工具）只依赖 `device` 这个抽象基类，从而与具体硬件解耦。

#### 4.1.2 核心流程

`device` 类对外暴露的关键类型和方法可以归纳为这张图：

```
            ┌─────────────── uhd::device（抽象基类）───────────────┐
            │                                                      │
   类型别名  │  sptr        = shared_ptr<device>                    │
            │  find_t      = device_addrs_t(const device_addr_t&)  │
            │  make_t      = sptr(const device_addr_t&)            │
            │  device_filter_t { ANY, USRP, CLOCK }                │
            │                                                      │
   三件套   │  static register_device(find, make, filter)          │
            │  static find(hint, filter=ANY) -> device_addrs_t     │
            │  static make(hint, filter=ANY, which=0) -> sptr      │
            │                                                      │
   通用能力  │  get_rx_stream / get_tx_stream / recv_async_msg      │
            │  get_tree() / get_device_type()                      │
            └──────────────────────────────────────────────────────┘
```

`find_t` 和 `make_t` 是两个函数类型（`std::function`）。每种具体硬件都会提供一对符合这两个签名的函数，再通过 `register_device` 登记进来。`device_filter_t` 是个三值枚举，用于在 `find`/`make` 时按设备大类过滤。

#### 4.1.3 源码精读

先看类型别名与过滤器枚举：

- `sptr`/`find_t`/`make_t` 三个类型别名在 [host/include/uhd/device.hpp:29-31](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L29-L31) 定义。注意 `find_t` 返回的是一组地址（`device_addrs_t`），而 `make_t` 返回单个设备指针——这正对应「发现可以有多个，制造只挑一个」。
- `device_filter_t` 枚举只有三个值 [`ANY, USRP, CLOCK`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L33-L35)。注释写明它「used as a filter in make」。
- 三件套的声明：[`register_device`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L44-L45)、[`find`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L57)、[`make`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L73-L74)。`make` 比 `find` 多一个 `which` 参数——当 `find` 找到多台设备时，`which` 指定用第几个（默认 0，即第一个）。
- 受保护成员 [`_tree` 和 `_type`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L117-L119)：`_tree` 是属性树（property_tree，下一阶段 [u2-l4](u2-l4-property-tree.md) 会讲），`_type` 记录自己属于哪一类设备，由 `get_device_type()` 返回。

`register_device` 的文档注释非常值得读：它点明了「登记一个设备到发现与工厂系统中」，三个参数分别是「发现函数、制造函数、过滤器」。

#### 4.1.4 代码实践

**实践目标**：通过公共头文件熟悉三件套的签名，不碰实现细节。

**操作步骤**：

1. 打开 `host/include/uhd/device.hpp`。
2. 定位 `device_filter_t` 枚举，确认它确实只有 `ANY`/`USRP`/`CLOCK` 三个值。
3. 对照 `find` 与 `make` 的声明，记录它们的返回类型和参数默认值。

**需要观察的现象**：`find` 的返回类型是 `device_addrs_t`（复数），`make` 的返回类型是 `sptr`（单个）；`make` 多出 `size_t which = 0`。

**预期结果**：你会得到一张「方法 → 返回类型 → 是否有默认参数」的对照表，作为后续阅读 `device.cpp` 的索引。

#### 4.1.5 小练习与答案

**练习 1**：`device_filter_t` 有哪三种取值？OctoClock 这类时钟设备应该用哪一个？

> **答案**：`ANY`、`USRP`、`CLOCK` 三种。OctoClock 是时钟设备，登记时用 `CLOCK`。

**练习 2**：`find_t` 和 `make_t` 的函数签名分别是什么？为什么 `find_t` 返回的是「一组」地址？

> **答案**：`find_t` 是 `device_addrs_t(const device_addr_t&)`，`make_t` 是 `device::sptr(const device_addr_t&)`。因为一次发现可能匹配到多台设备（比如同一网段里有多台 USRP），所以 `find_t` 返回一组地址供调用方选择，再由 `make` 从中挑一个去真正打开。

---

### 4.2 register_device 与设备自注册

#### 4.2.1 概念说明

`register_device` 是三件套里最关键、却最不起眼的一个。它的作用是：把「某一种具体硬件的 find 函数 + make 函数 + 过滤类别」打包成一个三元组，存进一个全局注册表（registry）。

那这些三元组是谁、在什么时候放进去的呢？答案是**每个设备实现文件自己**，而且是在**程序启动时自动完成**的——这就是「设备自注册」。`device.cpp` 里没有任何 `if (type == "b200")` 之类的分支，UHD 也不维护一张写死的设备清单。新增一种硬件，只需新写一个 `xxx_impl.cpp`，在里面用一个特殊的宏登记自己，并把它链接进 `libuhd`，它就会自动出现在发现结果里。

#### 4.2.2 核心流程

自注册靠两个宏和一个「构造时即执行」的小技巧实现：

```
程序启动（main 之前，动态初始化阶段）
        │
        │  每个设备实现文件里都有：
        │     UHD_STATIC_BLOCK(register_xxx_device) {
        │         device::register_device(&xxx_find, &xxx_make, device::USRP);
        │     }
        │
        ▼
  宏展开为：一个函数 register_xxx_device + 一个文件作用域静态对象 _fixture
        │
        ▼
  静态对象 _fixture 的构造函数被自动调用（在 main 之前）
        │
        ▼
  构造函数里执行 register_xxx_device()，即 register_device(...)
        │
        ▼
  三元组 (xxx_find, xxx_make, USRP) 被压入全局注册表 get_dev_fcn_regs()
```

关键点：**注册发生在 `main()` 执行之前**。等用户的 `main` 真正开始调用 `device::find` 时，注册表里已经躺好了所有「已链接」设备的三元组。

「已链接」三个字很重要：只有那些被编译进 `libuhd`（由 [u1-l3](u1-l3-build-system-cmake.md) 讲的组件机制决定）的设备实现才会自注册。比如禁用了 USB 传输组件，B200 的实现文件就不会被链接，自然也就不会出现在注册表里。

#### 4.2.3 源码精读

先看注册表的数据结构。在 `device.cpp` 里：

```cpp
// host/lib/device.cpp:64-67
typedef std::tuple<device::find_t, device::make_t, device::device_filter_t> dev_fcn_reg_t;
UHD_SINGLETON_FCN(std::vector<dev_fcn_reg_t>, get_dev_fcn_regs)
```

- `dev_fcn_reg_t` 就是一个三元组（[host/lib/device.cpp:64-67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L64-L67)）：发现函数、制造函数、过滤器。
- `UHD_SINGLETON_FCN` 是一个宏，它生成一个返回「该 vector 引用」的函数 `get_dev_fcn_regs()`。这个 vector 就是全局注册表本身。

`UHD_SINGLETON_FCN` 的展开见 [host/include/uhd/utils/static.hpp:18-23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/static.hpp#L18-L23)。它用的是「构造在首次使用」（construct on first use）惯用法——函数内一个 `static` 局部变量，第一次调用时才构造，且 C++11 起这种「魔法静态变量」的初始化是线程安全的。

`register_device` 的实现极其简单，就是把三元组 push 进注册表：

```cpp
// host/lib/device.cpp:69-74
void device::register_device(
    const find_t& find, const make_t& make, const device_filter_t filter)
{
    get_dev_fcn_regs().push_back(dev_fcn_reg_t(find, make, filter));
}
```

完整链接：[host/lib/device.cpp:69-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L69-L74)。

那么注册是何时发生的？看自注册宏 `UHD_STATIC_BLOCK`，它展开为「声明一个函数 + 定义一个文件作用域的静态 fixture 对象」：[host/include/uhd/utils/static.hpp:30-39](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/static.hpp#L30-L39)。而 fixture 对象的构造函数（在 `static.cpp` 里）会**直接调用**那个被登记的函数：

```cpp
// host/lib/utils/static.cpp:11-16
_uhd_static_fixture::_uhd_static_fixture(void (*fcn)(void), const char* name)
{
    try {
        fcn();                       // ← 在构造时就调用注册函数
    } catch (const std::exception& e) {
        std::cerr << "Exception in static block " << name << std::endl;
        ...
```

因为每个设备实现文件里的 `_fixture` 是「文件作用域的静态对象」，它会在程序动态初始化阶段（即 `main` 之前）被构造，于是 `fcn()`（也就是 `register_xxx_device`）就在 `main` 之前被执行了。构造函数还包了 `try/catch`，确保某个设备的注册失败（比如抛异常）不会让整个程序崩掉，而是打印到 stderr 后继续。

以 B200 为例，它的自注册代码长这样：

```cpp
// host/lib/usrp/b200/b200_impl.cpp:295-298
UHD_STATIC_BLOCK(register_b200_device)
{
    device::register_device(&b200_find, &b200_make, device::USRP);
}
```

其中 `b200_find` 和 `b200_make` 是同文件里定义的两个静态函数，签名正好匹配 `find_t` 与 `make_t`（见 [host/lib/usrp/b200/b200_impl.cpp:179](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b200/b200_impl.cpp#L179) 与 [host/lib/usrp/b200/b200_impl.cpp:272](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b200/b200_impl.cpp#L272)）。

#### 4.2.4 代码实践

这正是本讲规格里要求的核心实践：**在源码中找到所有调用 `register_device` 的设备实现文件，列出它们各自对应的设备类型。**

**操作步骤**：

1. 在仓库根目录用搜索工具查找 `register_device` 的调用点（限定在 `host/lib/` 下的 `.cpp` 文件）。
2. 对每个命中文件，定位它所在的 `UHD_STATIC_BLOCK`，读出三件事：静态块名、传入的 `find`/`make` 函数名、第三个参数 `filter`（`USRP` 还是 `CLOCK`）。
3. 把结果整理成一张表。

**需要观察的现象**：绝大多数设备用 `device::USRP`，只有一处用 `device::CLOCK`；`find`/`make` 的命名都遵循 `<设备>_find` / `<设备>_make` 的规律。

**预期结果**（基于当前 HEAD，共 8 个实现文件登记了设备）：

| 设备实现文件 | 静态块名 | find/make | filter | 对应硬件类型 |
|--------------|----------|-----------|--------|--------------|
| `host/lib/usrp/b100/b100_impl.cpp:139` | `register_b100_device` | `b100_find`/`b100_make` | `USRP` | USRP B100（旧款） |
| `host/lib/usrp/b200/b200_impl.cpp:297` | `register_b200_device` | `b200_find`/`b200_make` | `USRP` | USRP B200/B210/B200mini（USB） |
| `host/lib/usrp/usrp1/usrp1_impl.cpp:143` | `register_usrp1_device` | `usrp1_find`/`usrp1_make` | `USRP` | USRP1（最早期） |
| `host/lib/usrp/usrp2/usrp2_impl.cpp:190` | `register_usrp2_device` | `usrp2_find`/`usrp2_make` | `USRP` | USRP2/N series（旧以太网） |
| `host/lib/usrp/x300/x300_impl.cpp:164` | `register_x300_device` | `x300_find`/`x300_make` | `USRP` | USRP X3xx/X4xx（高性能以太网/PCIe） |
| `host/lib/usrp/mpmd/mpmd_impl.cpp:273` | `register_mpmd_device` | `mpmd_find`/`mpmd_make` | `USRP` | MPMD 设备（N3xx/X410 等现代设备，[u4-l4](u4-l4-mpmd-device.md) 详讲） |
| `host/lib/usrp/mpmd/sim_find.cpp:176` | `register_sim_device` | `sim_find`/`sim_make` | `USRP` | 模拟器设备（无硬件也能跑） |
| `host/lib/usrp_clock/octoclock/octoclock_impl.cpp:176` | `register_octoclock_device` | `octoclock_find`/`octoclock_make` | `CLOCK` | OctoClock 时钟分配器（唯一非 USRP） |

> 说明：表中行号对应 `register_device` 调用所在行。各设备的 `find`/`make` 具体如何探测硬件不在本讲范围，留待后续讲义（如 [u4-l4](u4-l4-mpmd-device.md) 讲 MPMD）。

#### 4.2.5 小练习与答案

**练习 1**：`UHD_STATIC_BLOCK(register_b200_device)` 宏展开后做了哪两件事？

> **答案**：① 声明一个无参 `void` 函数 `register_b200_device`（函数体就是你在宏后面写的 `{ ... }`）；② 定义一个文件作用域的静态对象 `register_b200_device_fixture`，类型是 `_uhd_static_fixture`，其构造函数会调用 `register_b200_device`。

**练习 2**：为什么 `register_device` 会在 `main()` 之前被执行，而用户代码里却看不到任何调用它的语句？

> **答案**：因为每个设备实现文件里的 `_fixture` 是文件作用域的静态对象，C++ 规定这类对象在程序动态初始化阶段（早于 `main`）构造。而它的构造函数（`static.cpp`）里直接调用了注册函数。所以只要某个设备实现文件被链接进 `libuhd`，它的注册就会自动发生，无需用户显式调用。

**练习 3**：如果我把 USB 传输组件在 CMake 里禁用，B200 还会出现在 `device::find` 的结果里吗？为什么？

> **答案**：不会。禁用 USB 组件会导致 B200 的实现文件不被编译/链接进 `libuhd`，于是那个 `UHD_STATIC_BLOCK` 根本不存在于最终二进制里，自注册不会发生，注册表里也就没有 B200 的三元组。

---

### 4.3 find：并行发现、过滤与去重

#### 4.3.1 概念说明

`device::find` 的职责是：遍历注册表里所有（符合 filter 的）设备类型，让每一种去「探测」自己的硬件，把找到的设备地址汇总，再按 hint 过滤、去重，返回给调用方。它**不真正打开设备**，只看「有没有、是什么」。

`find` 是 `uhd_find_devices` 命令令行工具的底层入口（见 [u1-l5](u1-l5-cli-tools.md)）。

#### 4.3.2 核心流程

```
find(hint, filter):
  lock(_device_mutex)                              # 串行化整个发现过程
  │
  ├─ 对注册表里每个三元组 (find_fn, make_fn, dev_filter):
  │     若 filter == ANY 或 dev_filter == filter:
  │         用 std::async 并发调用 find_fn(hint)    # 每种设备一个异步任务
  │
  ├─ 收集所有异步任务的结果:
  │     每个返回的 device_addr_t 配上它的 make_fn   # 形成 (地址, make) 对
  │     单个任务抛异常 → 打 ERROR 日志后跳过，不影响其它设备
  │
  ├─ 按 hint 里的 name/serial/type/product 过滤:
  │     hint 没填的字段不过滤；hint 填了的必须匹配
  │     特例：hint["type"] == "sim" 直接放行（模拟器）
  │
  ├─ 哈希去重:
  │     对每个地址算 hash_device_addr，用 set 去掉重复项
  │
  └─ 返回去重后的地址列表（不带 make_fn）
```

两个值得注意的设计：

- **并发探测**：不同设备走不同传输（B200 走 USB、X300 走网口），逐个串行探测会浪费时间，所以用 `std::async` 让它们并发跑。
- **去重**：以太网设备可能因为收到多次广播而出现重复条目（[u1-l5](u1-l5-cli-tools.md) 提过 `uhd_find_devices` 按 serial 合并），这里用哈希在底层再做一次去重。

#### 4.3.3 源码精读

并发探测发生在 `discover_devices_with_makers`：

```cpp
// host/lib/device.cpp:93-99（节选）
for (const auto& fcn : get_dev_fcn_regs()) {
    if (filter == device::ANY or std::get<2>(fcn) == filter) {        // ① filter 判断
        tasks.emplace_back(std::async(std::launch::async,
                              [fcn, hint]() { return std::get<0>(fcn)(hint); }), // ② 异步调用 find_fn
            std::get<1>(fcn));                                        // ③ 记下对应的 make_fn
    }
}
```

完整上下文见 [host/lib/device.cpp:86-114](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L86-L114)。第 ① 步正是 filter 的作用：当 `filter == USRP` 时，`CLOCK` 类型的 OctoClock 根本不会被探测。第 ② 步用 `std::launch::async` 强制新线程并发执行。收集结果时，[host/lib/device.cpp:102-111](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L102-L111) 对每个任务的 `.get()` 套了 `try/catch`，单个设备探测失败只记一条 `Device discovery error` 日志，不会中断整体发现。

按 hint 过滤的逻辑在 `find_filtered_devices_with_makers`：

```cpp
// host/lib/device.cpp:131-138（节选）
if ((not hint.has_key("name")    or hint["name"]    == discovered_addr["name"])
 and (not hint.has_key("serial") or utils::serial_numbers_match(hint["serial"], discovered_addr["serial"]))
 and (not hint.has_key("type")   or hint["type"]    == discovered_addr["type"]
                              or hint["type"] == "sim")   // 模拟器特例
 and (not hint.has_key("product") or not discovered_addr.has_key("product")
                              or hint["product"] == discovered_addr["product"])) {
```

规律是「hint 里没填的字段 = 不限制；填了的 = 必须相等」。完整段在 [host/lib/device.cpp:117-163](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L117-L163)。`type == "sim"` 是模拟器的特殊放行通道。

去重用哈希：

```cpp
// host/lib/device.cpp:150-160（节选）
std::set<size_t> device_hashes;
filtered_pairs.erase(
    std::remove_if(..., [&device_hashes](const auto& pair) {
        size_t hash       = hash_device_addr(std::get<0>(pair));
        const bool result = device_hashes.count(hash);   // 见过就标记删除
        device_hashes.insert(hash);
        return result;
    }), filtered_pairs.end());
```

哈希函数 `hash_device_addr`（[host/lib/device.cpp:36-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L36-L59)）把地址里排序后的键值对用 `boost::hash_combine` 逐个揉进一个 `size_t`，但会主动剔除 `claimed`/`skip_dram`/`skip_ddc`/`skip_duc` 这些「临时状态」键（黑名单），保证同一台设备无论是否被占用都算同一个哈希。

最后 `device::find` 本身很薄：加锁、调上面的过滤函数、把结果里的 `(地址, make)` 对剥成纯地址列表返回：[host/lib/device.cpp:165-180](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L165-L180)。

#### 4.3.4 代码实践

**实践目标**：用源码阅读理解「为什么 `uhd_find_devices` 不加任何参数就能列出所有设备」。

**操作步骤**：

1. 打开 `host/utils/uhd_find_devices.cpp`（[u1-l5](u1-l5-cli-tools.md) 已导览过它的骨架）。
2. 找到它调用 `device::find` 的地方，看传入的 hint 和 filter 是什么。
3. 回到 `device.cpp` 的 `discover_devices_with_makers`，确认：当 hint 为空、filter 为 `ANY` 时，所有注册表里的设备都会被探测。

**需要观察的现象**：hint 为空时，`hint.has_key("serial")` 等全部为 false，`find_filtered_devices_with_makers` 里那些 `not hint.has_key(...)` 全部为 true，于是**不过滤任何字段**，所有被发现的设备都会保留。

**预期结果**：你能说清「空 hint + ANY filter = 探测所有已链接设备、不做字段过滤」这条链路。**待本地验证**：在没有硬件的机器上，可以用模拟器验证——构造 `device_addr_t("type=sim")` 调用 `find`，应能返回一个模拟设备地址。

#### 4.3.5 小练习与答案

**练习 1**：为什么发现阶段要用 `std::async` 并发调用各设备的 `find` 函数，而不是一个个串行调用？

> **答案**：不同设备走不同传输介质（USB、以太网、PCIe），单个设备的探测可能因超时而较慢。并发探测让各设备同时进行，缩短总发现时间。同时每个任务独立 `try/catch`，一台设备探测失败或超时不会拖累其它设备。

**练习 2**：`hash_device_addr` 为什么要维护一个「黑名单」（`claimed`、`skip_dram` 等）？

> **答案**：这些键是设备的临时运行状态（如是否被占用 `claimed`），不代表设备身份。如果让它们参与哈希，同一台设备在「空闲」和「被占用」时会算出不同的哈希，去重就会失效，导致 `find` 返回重复条目。把它们排除后，同一台设备始终对应同一个哈希。

---

### 4.4 make：制造、哈希去重与设备复用

#### 4.4.1 概念说明

`device::make` 是真正的「重活」：它根据 hint 找到目标设备，调用对应的 `make` 函数真正打开硬件、分配传输资源、构造设备对象并返回。它是 `multi_usrp::make`、`uhd_usrp_probe` 等的底层入口。

`make` 有两个相比 `find` 多出来的精妙设计：

- **设备复用**：对「同一台设备」连续两次 `make`，第二次不会重新打开硬件，而是返回第一次创建的那个对象的共享指针。判断「同一台」靠的就是上文的设备哈希。
- **配置文件合并**：在调用真正的 `make` 函数前，会把用户配置文件里的额外参数（如缓冲区大小）合并进地址。

#### 4.4.2 核心流程

```
make(hint, filter, which):
  lock(_device_mutex)
  │
  ├─ pairs = find_filtered_devices_with_makers(hint, filter)   # 复用 find 的发现+过滤+去重
  │
  ├─ 错误检查:
  │     pairs 为空   → throw key_error("No devices found for ...")
  │     which >= 大小 → throw index_error("No device at index ...")
  │
  ├─ (dev_addr, maker) = pairs[which]
  ├─ dev_hash = hash_device_addr(dev_addr)
  │
  ├─ 把 hint 里有、但 dev_addr 里没有的键复制进 dev_addr
  │     （让调用方传额外传输参数，如 recv_frame_size）
  │
  ├─ 查设备缓存 hash_to_device (哈希 → weak_ptr):
  │     若已有且 weak_ptr 仍可 lock() → 直接返回那个已存在的设备对象
  │
  └─ 否则:
        dev = maker(prefs::get_usrp_args(dev_addr))   # 合并配置文件后真正制造
        hash_to_device[dev_hash] = dev                 # 用 weak_ptr 缓存
        return dev
```

#### 4.4.3 源码精读

`device::make` 的全貌在 [host/lib/device.cpp:185-242](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L185-L242)。几个关键片段：

**① 错误检查**——找不到设备或索引越界都抛异常：

```cpp
// host/lib/device.cpp:200-210（节选）
if (dev_addr_makers.empty()) {
    throw uhd::key_error(
        std::string("No devices found for ----->\n") + hint.to_pp_string());
}
if (dev_addr_makers.size() <= which) {
    throw uhd::index_error("No device at index " + std::to_string(which) + ...);
}
```

这就是为什么 `multi_usrp::make` 找不到设备时会抛 `key_error`。

**② 合并额外参数**——把 hint 里 dev_addr 没有的键补进去：

```cpp
// host/lib/device.cpp:219-224
for (const std::string& key : hint.keys()) {
    if (not dev_addr.has_key(key))
        dev_addr[key] = hint[key];
}
```

`find` 返回的 `dev_addr` 只包含发现阶段得到的关键字段，而用户在 hint 里可能还塞了额外的传输调优参数。这一步把它们带过去，保证后续 `maker` 能拿到。

**③ 设备复用**——基于哈希的 weak_ptr 缓存：

```cpp
// host/lib/device.cpp:227-241（节选）
static uhd::dict<size_t, std::weak_ptr<device>> hash_to_device;

if (hash_to_device.has_key(dev_hash)) {
    if (device::sptr p = hash_to_device[dev_hash].lock()) {   # 还活着 → 复用
        return p;
    }
}
device::sptr dev = maker(prefs::get_usrp_args(dev_addr));     # 否则真正制造
hash_to_device[dev_hash] = dev;                               # 用 weak_ptr 记下
return dev;
```

`hash_to_device` 是函数内 `static` 字典，用 `weak_ptr` 而非 `shared_ptr` 缓存——这样**不会阻止设备被析构**：当所有 `shared_ptr` 释放后设备照常销毁，缓存里的 `weak_ptr` 自动失效（`.lock()` 返回空），下次 `make` 会重新创建。注意 [host/lib/device.cpp:239](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L239) 这一行：真正的制造函数收到的不是原始 `dev_addr`，而是 `prefs::get_usrp_args(dev_addr)` 的结果——它会再合并一次用户配置文件（`uhd.conf` 之类）里的参数。

#### 4.4.4 代码实践

**实践目标**：理解「设备复用」带来的一个真实后果——对同一台设备，两个 `device::sptr` 其实指向同一个对象。

**操作步骤**：

1. 阅读 [host/lib/device.cpp:227-241](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L227-L241)，确认：只要 `hash_to_device[dev_hash].lock()` 成功，就直接 `return p`，**不会**再调用 `maker`。
2. 假设写下面这段示例代码（**示例代码**，非项目原有，演示用）：

   ```cpp
   // 示例代码：连续两次 make 同一台设备
   uhd::device_addr_t hint;
   hint["type"] = "sim";                       // 用模拟器，无需真实硬件
   auto dev1 = uhd::device::make(hint);
   auto dev2 = uhd::device::make(hint);        // 同一 hint → 同一哈希
   // dev1.get() == dev2.get()  期望为 true：指向同一对象
   ```

3. 推断 `dev1.get() == dev2.get()` 的结果。

**需要观察的现象**：第二次 `make` 命中缓存，`weak_ptr` 还能 `lock()`（因为 `dev1` 仍持有 `shared_ptr`），于是直接返回 `dev1` 所指对象。

**预期结果**：两次 `make` 返回的 `shared_ptr` 指向同一个底层 `device` 对象（`dev1.get() == dev2.get()` 为 `true`），`maker` 只被调用一次。**待本地验证**（需编译 UHD 并启用模拟器组件）。

#### 4.4.5 小练习与答案

**练习 1**：`make` 如何避免对同一台设备重复打开硬件？

> **答案**：用一个静态字典 `hash_to_device`（哈希 → `weak_ptr<device>`）做缓存。`make` 时先算设备哈希，若缓存里存在且 `weak_ptr` 还能 `lock()` 成功（说明前一个 `shared_ptr` 仍存活），就直接返回那个已有对象，不再调用 `maker`。

**练习 2**：为什么 `hash_to_device` 用 `weak_ptr` 而不是 `shared_ptr` 来缓存设备？

> **答案**：用 `shared_ptr` 会一直持有设备、阻止其析构，造成设备永远无法释放（即使所有用户都已放手）。用 `weak_ptr` 只做「观察」：当所有 `shared_ptr` 释放后设备正常销毁，`weak_ptr` 自动失效，下次 `make` 才会重新创建。

**练习 3**：当 `make` 找不到任何匹配设备时，抛的是什么异常？这和你 [u1-l6](u1-l6-first-example-rx-to-file.md) 里看到的什么现象对应？

> **答案**：抛 `uhd::key_error`（[device.cpp:202](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L202)）。这会向上传播，最终被示例里的 `UHD_SAFE_MAIN` 兜住，打印成 `Error: ...` 并以非零退出码退出——正是 `rx_samples_to_file` 找不到设备时的表现。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路追踪」任务。

**任务**：选定一个具体设备（推荐 B200，因为它最典型），画出从**程序启动**到 `device::make` **返回**的完整时序，并解释每一步发生在哪里。

**建议步骤**：

1. **注册阶段**（`main` 之前）：读 [host/lib/usrp/b200/b200_impl.cpp:295-298](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b200/b200_impl.cpp#L295-L298)，写出 `UHD_STATIC_BLOCK(register_b200_device)` 如何经由 `_uhd_static_fixture` 构造（[static.cpp:11-16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/static.cpp#L11-L16)）把 `(b200_find, b200_make, USRP)` 压进 `get_dev_fcn_regs()`（[device.cpp:69-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L69-L74)）。

2. **发现阶段**：假设用户调用 `device::make(b200_hint, device::USRP)`。追踪 [device.cpp:185-190](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L185-L190) → `find_filtered_devices_with_makers` → `discover_devices_with_makers`（[device.cpp:86-114](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L86-L114)），标出 `b200_find` 被 `std::async` 调用、filter 判断（`USRP==USRP` 通过）、其它 `CLOCK`/不匹配设备被跳过的位置。

3. **过滤+去重阶段**：在 [device.cpp:117-163](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L117-L163) 标出按 serial 过滤、按 `hash_device_addr` 去重的两段。

4. **制造阶段**：在 [device.cpp:200-241](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L200-L241) 标出：错误检查 → 选第 `which` 个 → 合并 hint 额外键 → 查 `hash_to_device` 缓存 → 调用 `maker(prefs::get_usrp_args(dev_addr))`，最终 `b200_make`（[b200_impl.cpp:272](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b200/b200_impl.cpp#L272)）真正打开 USB 设备。

**交付物**：一张带时间轴的时序图（文字版即可），把「`main` 之前的自注册」和「`main` 里的 make 调用」两段时间清楚分开，并标注每个箭头对应的源码行号。如果你有硬件或启用了模拟器，可以在关键点（如 `b200_find`、`b200_make`）加一行日志验证时序；否则标明「源码阅读型实践」。

## 6. 本讲小结

- `uhd::device` 是所有设备的抽象基类，对外暴露三件套：`register_device`（登记）、`find`（发现）、`make`（制造），外加 `device_filter_t {ANY, USRP, CLOCK}` 三值过滤器。
- 设备**自注册**靠 `UHD_STATIC_BLOCK` 宏：每个实现文件里定义一个文件作用域静态 `_uhd_static_fixture`，它在 `main` 之前的动态初始化阶段构造，构造函数里调用 `register_device` 把 `(find, make, filter)` 三元组压进全局注册表 `get_dev_fcn_regs()`。
- `find` 用 `std::async` **并发探测**所有符合 filter 的设备类型，按 hint 的 `name/serial/type/product` 字段过滤，再用 `hash_device_addr` **去重**，最后返回纯地址列表（不打开设备）。
- `make` 复用 `find` 的发现结果，校验后用 `which` 选定一个，合并 hint 的额外参数，再通过 `hash_to_device`（哈希→`weak_ptr`）**复用**已打开的同台设备；找不到抛 `key_error`，越界抛 `index_error`。
- 当前 HEAD 共有 8 个设备实现文件调用 `register_device`：7 个 `USRP`（B100/B200/USRP1/USRP2/X300/MPMD/Sim），1 个 `CLOCK`（OctoClock）。
- 只有被 CMake 组件机制**链接进 `libuhd`** 的实现才会自注册——这是工厂机制与构建系统（[u1-l3](u1-l3-build-system-cmake.md)）的接合点。

## 7. 下一步学习建议

本讲只解释了「地址容器」的用法，却没有讲清它的结构。下一讲 **[u2-l2 设备地址 device_addr_t 与传输类型](u2-l2-device-addr.md)** 会深入 `device_addr_t` 这个键值对容器：它有哪些约定俗成的键（`type`/`addr`/`serial`/`resource` 等）、`device_filter_t` 在 `make` 里如何配合、以及设备哈希为何要排除某些键。

如果想提前看到「某种设备的 find/make 具体怎么探测硬件」，可以跳读：

- `host/lib/usrp/b200/b200_impl.cpp` 里的 `b200_find` / `b200_make`（USB 设备的典型实现，最易读）。
- `host/lib/usrp/mpmd/mpmd_impl.cpp` 里的 `mpmd_find` / `mpmd_make`（现代设备的代表，[u4-l4](u4-l4-mpmd-device.md) 会专讲）。

而如果你想理解 `device::make` 返回的对象之上那层易用封装，请继续到 **[u2-l3 multi_usrp 高层 API](u2-l3-multi-usrp-api.md)**。
