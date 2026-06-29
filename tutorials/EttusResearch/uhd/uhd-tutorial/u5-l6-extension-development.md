# 扩展开发与二次开发指南

> 所属单元：u5 扩展、绑定与工程化 ｜ 阶段：advanced ｜ 依赖：u3-l6（常用 RFNoC 块）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 UHD 的 **extension（扩展）框架**解决什么问题：在不动 UHD 主干代码、不重新编译 `libuhd` 的前提下，把一个外部射频前端「插」进 USRP 的控制链路。
- 画出一条扩展从「注册 → 发现 → 加载 → 实例化 → 被 `multi_usrp` 调用」的完整生命周期，并指出每一步对应的源码位置。
- 读懂 `extension_example` 这个官方示例的**接口 / 实现 / 构建三件套**，并能照着它写出自己的最小扩展。
- 知道向 UHD 官方仓库贡献代码时必须遵守的**编码规范与法律流程**（CLA、clang-format、commit 风格）。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **`multi_usrp` 高层 API**（u2-l3）：它是用户最常调用的「设备黑盒」，所有 `set_rx_gain`/`set_rx_freq` 等方法最终都会落到一个射频控制接口上。本讲要讲的就是：**这个接口可以被你的扩展替换**。
- **`property_tree` 属性树**（u2-l4）与 **experts 属性传播框架**（u3-l5）：扩展示例正是用 experts 演示「改一个属性 → 自动重算另一个属性」的。
- **RFNoC 与 `radio_control`**（u3-l1、u3-l6）：扩展是挂在某个 `radio_control` 块上的，必须理解「块」的概念。

两个术语先澄清：

| 术语 | 含义 |
|---|---|
| **extension（扩展）** | 一个独立的 `.so`/`.dll` 共享库，实现 `uhd::extension::extension` 基类，用来给 USRP 附加自定义射频前端控制逻辑。 |
| **模块（module）** | UHD 在运行期通过 `dlopen`/`LoadLibrary` 动态加载的共享库文件。扩展就是一种模块。 |

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `host/include/uhd/extension/extension.hpp` | 扩展**公共基类**与自注册宏 `UHD_REGISTER_EXTENSION`（公共头，用户可见）。 |
| `host/lib/extension/extension.cpp` | 扩展**注册表**（registry）与工厂查找 `get_extension_factory` 的实现（编进 `libuhd`）。 |
| `host/lib/include/uhdlib/extension/extension_factory.hpp` | 工厂类声明（内部头）。 |
| `host/lib/utils/load_modules.cpp` | 运行期 **`dlopen` 加载**所有模块的入口。 |
| `host/lib/utils/paths.cpp` | 计算 `UHD_MODULE_PATH` 等模块搜索路径。 |
| `host/lib/usrp/multi_usrp_rfnoc.cpp` | `multi_usrp` **实例化扩展并把射频接口挂钩换成扩展**的地方。 |
| `host/examples/extension_example/` | 官方**最小扩展示例**（接口 + 实现 + CMake）。 |
| `host/docs/driver_usage/extension.dox` | 扩展框架的官方文档页。 |
| `CODING.md` / `CONTRIBUTING.md` | 编码规范与贡献流程。 |

## 4. 核心概念与源码讲解

### 4.1 extension：扩展机制总览

#### 4.1.1 概念说明

设想你做了一个外接的射频前端（比如一个专用放大器、一个定制天线开关、或者一块非 Ettus 出品的子板），你想：

1. 用 USRP 收发，但**射频参数的计算逻辑是你自己的**（例如增益要按你的前端特性曲线折算）。
2. 仍然通过 `multi_usrp::set_rx_gain()` 这种**用户已经熟悉的统一 API** 来控制，而不是另起一套接口。

UHD 的 extension 框架就是为这个场景设计的：它定义一个基类 `uhd::extension::extension`，你的扩展继承它、实现一批射频控制方法（频率、增益、带宽、天线……），然后**把扩展实例塞进 `multi_usrp` 的射频控制链路里**，让 `multi_usrp` 调用你的方法而不是 radio 块的原生方法。

关键设计目标是**解耦**：

- 你的扩展代码**不在 `libuhd` 仓库里**，而是单独编译成一个共享库。
- `libuhd` 启动时**不知道**有哪些扩展，只在运行期去固定目录（`UHD_MODULE_PATH`）里 `dlopen` 所有 `.so`。
- 每个扩展自己用宏 `UHD_REGISTER_EXTENSION` **自注册**到一个全局注册表里。
- 用户在创建 `multi_usrp` 时传一个 `extension=<名字>` 参数，`libuhd` 据此到注册表里查工厂函数、实例化扩展。

#### 4.1.2 核心流程

一条扩展从「写好」到「被调用」的完整链路如下：

```
① 编译期
   你的扩展.cpp 末尾写 UHD_REGISTER_EXTENSION("foo", foo_extension)
        │  展开成一个 UHD_STATIC_BLOCK（main 之前执行）
        ▼
② 进程启动期（libuhd 被加载时）
   load_modules 这个 UHD_STATIC_BLOCK 运行
        │  遍历 UHD_MODULE_PATH 下的每个文件
        ▼
   dlopen("libfoo.so")  ── 触发 libfoo 的静态初始化
        │  执行 ① 中那个 static block
        ▼
   extension::register_extension("foo", foo_extension::make)
        │  往全局 registry 这个 unordered_map 里插入 {"foo" -> make函数}
        ▼
③ 运行期（用户创建设备）
   multi_usrp::make(device_addr) ，device_addr 里有 extension=foo
        │  调 extension_factory::get_extension_factory("foo")
        ▼
   从 registry 取出 make 函数，传入 factory_args{radio_ctrl, mb_ctrl}
        │  调用 foo_extension::make(fargs)
        ▼
   得到 extension::sptr，存进 multi_usrp 的 _extensions 表
        ▼
④ 调用期
   用户调 multi_usrp->set_rx_gain(...)
        │  该通道的射频核心被设成了扩展（dynamic_pointer_cast）
        ▼
   实际调用的是扩展的 set_rx_gain（可以是转发、改写或自定义逻辑）
```

后面三个小节分别精读「基类 + 注册表」「加载链路」「`multi_usrp` 挂钩」三段源码。

#### 4.1.3 源码精读

**(a) 扩展基类与自注册宏**

扩展的公共基类定义在 `extension.hpp`。它同时继承两个射频控制接口，这正是 `multi_usrp` 能用扩展替换 radio 的前提——扩展必须「长得像」一个射频控制器：

[host/include/uhd/extension/extension.hpp:21-49](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/extension/extension.hpp#L21-L49) —— 定义 `extension` 基类，多重继承 `core_iface` 与 `power_reference_iface`，并声明 `factory_args`（携带 `radio_ctrl` 和 `mb_ctrl` 两个指针）与 `factory_type`（一个返回 `sptr` 的工厂函数类型）。

文件末尾的宏是自注册的核心：

[host/include/uhd/extension/extension.hpp:60-64](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/extension/extension.hpp#L60-L64) —— `UHD_REGISTER_EXTENSION(NAME, CLASS_NAME)` 宏展开成一个 `UHD_STATIC_BLOCK`，在 `main()` 之前调用 `extension::register_extension(#NAME, CLASS_NAME::make)`。

这里用到的 `UHD_STATIC_BLOCK` 是 UHD 的「构造于首次使用之前」惯用法，定义如下：

[host/include/uhd/utils/static.hpp:30-33](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/static.hpp#L30-L33) —— 它声明一个函数，再用一个静态 `_uhd_static_fixture` 全局对象的构造函数去调用它；由于全局对象在 `main` 之前构造，所以注册逻辑会在程序入口前执行。

**(b) 注册表与工厂查找**

注册表本身就是一个进程级的 `unordered_map<名字, 工厂函数>`，用单例惯用法保证唯一：

[host/lib/extension/extension.cpp:15-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/extension.cpp#L15-L29) —— `UHD_SINGLETON_FCN` 定义一个惰性单例 `get_extension_registry()`；`register_extension` 往里插入键值对，若键已存在则打印警告并拒绝覆盖（防止重复注册）。

查找逻辑在同一个文件里：

[host/lib/extension/extension.cpp:35-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/extension.cpp#L35-L50) —— `get_extension_factory(name)` 找不到时返回 `nullptr`，并打印「已安装的扩展列表」帮助排错。注意它返回的是**工厂函数本身**（不是实例），调用方拿到后再自行 `factory(args)` 实例化。

`extension.cpp` 通过 `LIBUHD_APPEND_SOURCES` 被编进 `libuhd` 本体：

[host/lib/extension/CMakeLists.txt:10-12](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/CMakeLists.txt#L10-L12) —— 把 `extension.cpp` 追加进 `libuhd` 源文件列表。而公共头 `extension.hpp` 则经 `UHD_INSTALL` 安装给用户：

[host/include/uhd/extension/CMakeLists.txt:7-12](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/extension/CMakeLists.txt#L7-L12)。

**(c) 加载链路：`UHD_MODULE_PATH` 与 `dlopen`**

`libuhd` 启动时怎么知道去哪找扩展？答案在一个静态块里：

[host/lib/utils/load_modules.cpp:122-132](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/load_modules.cpp#L122-L132) —— `UHD_STATIC_BLOCK(load_modules)` 遍历 `get_module_paths()` 和 `get_module_d_paths()` 返回的所有目录，逐个加载。

实际的「加载」动作就是 `dlopen`（Linux）或 `LoadLibrary`（Windows）：

[host/lib/utils/load_modules.cpp:26-32](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/load_modules.cpp#L26-L32) —— `dlopen` 把共享库映射进进程地址空间；**正是这一步触发了共享库内全局对象的构造**，进而执行 `UHD_REGISTER_EXTENSION` 展开出的 static block，完成注册。换句话说：注册表的内容，取决于「哪些 `.so` 被加载了」。

搜索路径由环境变量和默认目录共同决定：

[host/lib/utils/paths.cpp:305-319](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/paths.cpp#L305-L319) —— `get_module_paths()` 依次收集：环境变量 `UHD_MODULE_PATH`、`<lib路径>/uhd/modules`、`<数据路径>/modules`。所以扩展库放进这三个位置之一即可被发现。

> 官方文档 [host/docs/driver_usage/extension.dox:104-110](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/docs/driver_usage/extension.dox#L104-L110) 把这条「放哪」的规则讲得更直白：`UHD_MODULE_PATH`、`<install>/share/uhd/modules`、或 `/usr/share/uhd/modules`。

**(d) `multi_usrp` 如何挂钩扩展**

注册只是「登记」，真正使用发生在 `multi_usrp` 构造时。当设备参数里带 `extension=foo`：

[host/lib/usrp/multi_usrp_rfnoc.cpp:221-235](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L221-L235) —— 读出 `extension` 名字 → `get_extension_factory` 取工厂 → 用 `{radio_blk, mb_ctrl}` 构造 `factory_args` → 调工厂得到扩展实例；找不到就抛 `value_error`。

拿到实例后，按「(radio_id, 方向, 通道)」三元组存进一张表：

[host/lib/usrp/multi_usrp_rfnoc.cpp:243-258](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L243-L258) —— 把扩展与每个 RX/TX 通道关联起来。

挂钩的「魔法」在这里——构建 RX 通道时，射频核心到底用谁：

[host/lib/usrp/multi_usrp_rfnoc.cpp:1189-1209](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L1189-L1209) —— 如果该通道有扩展，就把射频核心 `rf_core` `dynamic_pointer_cast` 成扩展；否则用 `radio_blk`。此后 `multi_usrp` 上所有 `set_rx_gain`/`set_rx_freq` 等调用都会走 `rf_core`，也就是**走你的扩展**。TX 通道在 1821 行附近做同样的事。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「注册表是空 map，靠 `dlopen` 填充」这一结论，不依赖硬件。

**操作步骤**：

1. 在仓库根目录统计所有使用自注册宏的扩展：

   ```bash
   grep -rn "UHD_REGISTER_EXTENSION" host/
   ```

   预期只命中示例那一处（`host/examples/extension_example/lib/extension_example.cpp`），因为这是仓库里唯一的扩展；真正的扩展都在仓库之外、由厂商各自编译。

2. 阅读 [host/lib/extension/extension.cpp:35-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/extension.cpp#L35-L50)，确认找不到扩展时它打印「Installed extensions:」列表——这个列表就是当前 `dlopen` 进来的所有扩展。

3. 跟踪 `UHD_STATIC_BLOCK(load_modules)`（[load_modules.cpp:122](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/load_modules.cpp#L122)），画出「静态块 → 遍历路径 → dlopen → 触发扩展 static block → register_extension」的因果链。

**需要观察的现象**：步骤 1 的 grep 结果数量极少（仅示例），印证「扩展是外挂的、不在主干」这一设计。

**预期结果**：能口头复述「`libuhd` 启动时注册表是空的，内容完全由 `UHD_MODULE_PATH` 下被 `dlopen` 的库决定」。

> 本步骤为纯源码阅读型实践；若要在真机验证，可编译 `extension_example` 后用 `extension=extension_example` 打开设备（见 4.2.4）。

#### 4.1.5 小练习与答案

**Q1**：如果你把同一个扩展库放进 `UHD_MODULE_PATH` 的两个不同目录，会发生什么？

**答**：`load_module_path` 会对两个目录各 `dlopen` 一次，于是 `register_extension` 被调用两次。第二次因键已存在，会触发 [extension.cpp:22-27](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/extension.cpp#L22-L27) 的 `WARNING` 并直接 `return`，不会覆盖。功能上仍正常，但日志里会有警告。

**Q2**：为什么 `extension` 基类要同时继承 `core_iface` 和 `power_reference_iface`？

**答**：因为 `multi_usrp` 是通过 `dynamic_pointer_cast<core_iface>(extension)` 来获得射频核心的（见 4.1.3(d)）。扩展必须实现这些接口，才能「冒充」一个可被 `multi_usrp` 调用的射频控制器，从而拦截频率/增益/功率等调用。

---

### 4.2 extension_example：示例的结构、实现与构建

#### 4.2.1 概念说明

`host/examples/extension_example/` 是官方给出的最小可工作扩展。它的作用不是真去控制什么硬件，而是**演示扩展能做的三件事**：

1. **转发**：把 `set_rx_bandwidth` 等方法原样转给底层 radio（最简单的情况）。
2. **改写**：例如 `set_tx_frequency` 把传入频率乘以 2 再下发——演示「扩展可以改写参数」。
3. **自定义逻辑 + experts 联动**：演示用 `experts` 框架在「设置增益」时自动重算（RX 加 3dB、TX 减 3dB）。

它由三部分组成：

| 层 | 文件 | 作用 |
|---|---|---|
| 接口 | `include/extension_example/extension_example.hpp` | 声明扩展**独有**的公共方法（如 `write_log`）。 |
| 实现 | `lib/extension_example.hpp` + `lib/extension_example.cpp` | 继承接口与若干 mixin，实现所有射频钩子，并注册。 |
| 构建 | `CMakeLists.txt`（顶层 + `lib/` + `include/`） | 编译成共享库 `libextexample.so` 并安装到模块目录。 |

#### 4.2.2 核心流程

写一个扩展的标准动作：

```
1. 定义接口类（继承 uhd::extension::extension），声明扩展独有的方法
2. 定义实现类（继承接口 + 必要的 mixin），实现所有 core_iface/power_reference_iface 纯虚函数
   ├─ 能转发的 → 转给 _radio
   ├─ 要改写的 → 自己算
   └─ 不支持的 → throw not_implemented_error
3. 实现静态工厂 make(factory_args)：从 fargs 取 radio_ctrl、get_tree()，构造实现对象
4. 在 .cpp 末尾写 UHD_REGISTER_EXTENSION(名字, 实现类)
5. CMake: find_package(UHD) → add_library(名字 SHARED ...) → 链接 UHD::uhd → 安装到 modules 目录
```

#### 4.2.3 源码精读

**(a) 接口类**

[host/examples/extension_example/include/extension_example/extension_example.hpp:21-42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/include/extension_example/extension_example.hpp#L21-L42) —— `extension_example` 继承 `uhd::extension::extension`，额外声明一个**扩展独有**的纯虚方法 `write_log`（[第 32 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/include/extension_example/extension_example.hpp#L32)），以及静态工厂 `make(factory_args)`（[第 41 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/include/extension_example/extension_example.hpp#L41)）。独有方法必须放在这里声明，因为基类不知道你的扩展还能干什么。

**(b) 工厂函数 make**

工厂是从「名字」到「实例」的桥梁，由注册表持有：

[host/examples/extension_example/lib/extension_example.cpp:16-22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.cpp#L16-L22) —— `make` 从 `fargs.radio_ctrl` 取出属性树，然后构造 `extension_example_impl`，把 radio 和 tree 传进去。

**(c) 三种钩子风格的实现**

实现类继承接口和两个 mixin（提供「无名字增益」「天线」等通用实现）：

[host/examples/extension_example/lib/extension_example.hpp:22-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.hpp#L22-L24)。

三种风格各举一例：

- **改写**（频率乘 2，纯演示）：

  [host/examples/extension_example/lib/extension_example.hpp:50-53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.hpp#L50-L53) —— `set_tx_frequency` 把 `freq` 乘以 2.0 再转给 radio，并把 radio 返回的「实际频率」回传。

- **自定义 + experts**（把增益写进属性树，触发专家重算）：

  [host/examples/extension_example/lib/extension_example.hpp:94-100](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.hpp#L94-L100) —— `set_tx_gain` 不直接调 radio，而是写属性树的 `gains/all/value`；这个写动作会触发下面 4.2.3(d) 的 `gain_expert`。

- **不支持**（明确抛异常）：

  [host/examples/extension_example/lib/extension_example.hpp:138-141](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.hpp#L138-L141) —— `set_rx_agc` 抛 `not_implemented_error`。文档注释提醒：抛异常会中断应用，需谨慎。

此外还可以**完全自定义返回值**，例如返回一个假频率范围用于演示：

[host/examples/extension_example/lib/extension_example.cpp:135-143](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.cpp#L135-L143) —— `get_tx_frequency_range` 返回 `(0, 100)`、`get_rx_frequency_range` 返回 `(0, 200)`，与 radio 无关。

**(d) 用 experts 演示属性联动**

这是示例最有教学价值的一段。构造时为每个通道、每个方向建一棵专家容器，并注册一个 `gain_expert`：

[host/examples/extension_example/lib/extension_example.cpp:83-131](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.cpp#L83-L131) —— `init_path` 用 `expert_factory::add_dual_prop_node` 把属性树节点绑成专家图的「desired/coerced」输入输出对，并用 `AUTO_RESOLVE_ON_WRITE` 让「写入即触发」；再 `add_worker_node<gain_expert>` 注册计算节点。

专家本身（RX 加 3dB、TX 减 3dB）：

[host/examples/extension_example/lib/extension_example.cpp:54-69](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.cpp#L54-L69) —— `resolve()` 读 `_gain_in`（用户写入的 desired），按方向加减 3dB 写到 `_gain_out`（coerced）。这正好承接 u3-l5 的 experts 框架。

**(e) 注册**

最后一行把扩展登记进注册表：

[host/examples/extension_example/lib/extension_example.cpp:150](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/lib/extension_example.cpp#L150) —— `UHD_REGISTER_EXTENSION(extension_example, ext_example::extension_example)`。第一个参数是加载时用的名字（`extension=extension_example`），第二个是含 `make` 的类。

**(f) CMake 构建**

顶层 CMake 的关键步骤：

[host/examples/extension_example/CMakeLists.txt:26-31](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/CMakeLists.txt#L26-L31) —— `find_package(UHD 4.3.0 REQUIRED)` 把已安装的 UHD 作为依赖引入（最低版本 4.3.0，这是扩展 API 可用的起点）。

编译成共享库并链接 UHD：

[host/examples/extension_example/CMakeLists.txt:62-76](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/CMakeLists.txt#L62-L76) —— `add_library(extexample SHARED ...)`，`target_link_libraries` 链 `${UHD_LIBRARIES}`。

安装到模块目录（Linux 用软链接、Windows 直接拷）：

[host/examples/extension_example/CMakeLists.txt:94-102](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/CMakeLists.txt#L94-L102) —— Linux 下 `install` 到 `lib/`，再 `ln -s` 到 `UHD_MODULE_PATH`，这样 `dlopen` 才能找到。

#### 4.2.4 代码实践

**实践目标**：把示例扩展真正编译、安装、加载，观察它如何改变 `multi_usrp` 的行为。

**操作步骤**：

1. 准备一个已安装好的 UHD（`uhd_config_info` 能正常输出）。
2. 编译示例（官方文档 [extension.dox:9-15](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/docs/driver_usage/extension.dox#L9-L15) 给的步骤）：

   ```bash
   cd host/examples/extension_example
   mkdir build && cd build
   cmake .. && make
   sudo make install      # 需要 root 才能写入 modules 目录
   ```

3. （替代方案，不想 sudo）不安装，而是临时指定搜索路径：

   ```bash
   export UHD_MODULE_PATH=/path/to/build/libextexample.so
   ```

   或指向含该 `.so` 的目录。

4. 用 `uhd_usrp_probe` 验证扩展被加载（需有 RFNoC 设备）：

   ```bash
   uhd_usrp_probe --args "extension=extension_example"
   ```

**需要观察的现象**：

- 构建设备时传入 `extension=extension_example`，若库未被发现，会命中 [multi_usrp_rfnoc.cpp:231-233](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L231-L233) 的 `Unrecognized extension` 报错。
- 若加载成功，调 `set_tx_freq(1e9)` 会因为「乘 2」逻辑实际下发 2e9（可用 `uhd_usrp_probe` 或小程序回读验证）。

**预期结果**：能复现「扩展改写了射频参数」这一行为。若手头无 RFNoC 设备，则标注「待本地验证」，改为阅读 4.2.3 的改写逻辑并推演结果。

> 提示：示例 CMake 自带 `set(CMAKE_CXX_STANDARD 11)`（[第 12 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/CMakeLists.txt#L12)），这只是示例默认值；按 CODING.md，贡献给 UHD 的代码可以用更现代的 C++ 特性。

#### 4.2.5 小练习与答案

**Q1**：示例里 `set_rx_agc` 抛 `not_implemented_error`，而 `get_rx_frequency` 只是转发给 radio。这两种处理方式分别适用于什么场景？

**答**：转发适用于「扩展不关心、直接用 radio 原生能力」的方法；抛异常适用于「该能力在你的前端上根本不存在」，尽早暴露错误比静默忽略更安全。注意抛异常会沿调用栈上抛，可能中断应用，故文档强调「use with caution」。

**Q2**：为什么 `make` 要从 `fargs.radio_ctrl->get_tree()` 取属性树，而不是自己新建一棵？

**答**：扩展必须和 radio **共享同一棵属性树**，否则 `gain_expert` 监听的节点与 `multi_usrp` 写入的节点不在同一棵树上，专家永远不会被触发。`factory_args` 传入的 `radio_ctrl` 已挂在一棵已存在的树上，`get_tree()` 拿到的就是它。

---

### 4.3 编码规范与贡献流程

#### 4.3.1 概念说明

前面两节讲的是「自己写扩展给自己用」，这一节讲「把代码贡献回 UHD 官方仓库」。Ettus Research / NI 对合并的代码有两类要求：

- **技术规范**（CODING.md）：格式、include 顺序、命名、commit 风格——保证代码库一致、可维护。
- **法律流程**（CONTRIBUTING.md）：非平凡贡献必须签 CLA，把版权转让给 Ettus/NI。

这两份文件**适用于仓库里所有代码**，不止 `libuhd`，也包括 `fpga/`、`mpm/` 等（FPGA 另有 `fpga/CODING.md`）。

#### 4.3.2 核心流程

贡献代码的标准动作：

```
1. （非平凡改动）先在邮件列表/issue 上沟通，避免白做
2. 签 CLA（<10 行或文档改动可豁免，由 Ettus 判定）
3. 写代码，遵守 CODING.md：
   ├─ 4 空格缩进、无 tab、无行尾空格
   ├─ 行宽 C/C++ ≤90、Python ≤100
   ├─ include 顺序：本地 → UHD → 第三方 → Boost → 标准
   ├─ .at() 优于 []、lambda 优于 std::bind、size_t 做索引
   └─ 与硬件交互用定长类型（int32_t 等）
4. 用 clang-format / black+isort（或 ni-python-styleguide）自动格式化
5. pre-commit install 装钩子，提交前自动检查
6. commit：命令式语气的标题（前缀子系统，≤50 字符），空行后写正文解释 what/why
7. 提 PR，走 review
```

#### 4.3.3 源码精读

**(a) C++ 编码要点**

[CODING.md:37-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L37-L59) —— 给出 C++ 规范要点：必须过 `.clang-format`；include 按「本地 → 其他 UHD → 第三方 → Boost → 标准」排序（理由：这样最能暴露缺失的 include）；优先标准库而非 Boost。

举一个仓库内的真实范例（前述 `extension_example.cpp` 的 include 块就符合这个顺序）：

```cpp
#include "extension_example.hpp"        // 本地
#include <uhd/experts/expert_container.hpp>  // 其他 UHD
#include <uhd/extension/extension.hpp>       // 其他 UHD
#include <uhd/utils/log.hpp>                 // 其他 UHD
#include <cassert>                           // 标准
```

索引与定长类型的规范：

[CODING.md:48-52](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L48-L52) —— 索引用 `size_t`（但注意其大小平台相关）；与硬件交互必须用 `int32_t` 等定长类型，否则极易踩到尺寸错误。

`.at()` 优先的规范：

[CODING.md:91-94](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L91-L94) —— map/vector 用 `.at()` 而非 `[]`，因为 `[]` 会默认构造一个值，而 `.at()` 在键不存在时抛异常——后者通常才是期望行为。对照 [extension.cpp:49](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/extension/extension.cpp#L49) 的 `get_extension_registry().at(ext_name)`，正是这条规范的实际应用。

**(b) Commit 风格**

[CODING.md:136-153](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L136-L153) —— 几乎只用 fast-forward 合并、无 merge commit；标题用「子系统前缀 + 命令式语气」（如 `extension: Add gain coercion expert`），≤50 字符、硬上限 72；正文空一行后写，解释 what/why（how 应能从 diff 看出）；**不要把行为修改和格式清理混在同一个 commit**。本仓库最近的提交（如 `images: Update to final 4.10.0.0 release candidate`）就是这种风格。

**(c) 工具链**

[CODING.md:157-186](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L157-L186) —— C/C++ 用 `clang-format`，Python 用 `black`+`isort` 或 `ni-python-styleguide`；推荐 `pre-commit install` 装 git 钩子，提交前自动扫描格式问题。

**(d) 法律流程**

[CONTRIBUTING.md:49-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CONTRIBUTING.md#L49-L59) —— 非平凡贡献需签 [CLA](http://files.ettus.com/licenses/Ettus_CLA.pdf)；小于 10 行或纯文档改动可免 CLA，由 Ettus/NI 判定。Ettus 保留拒绝任何提交的权利，故大改动前**务必先沟通**。

#### 4.3.4 代码实践

**实践目标**：体验官方工具链对一段代码的格式约束。

**操作步骤**：

1. 找一份仓库里的 `.clang-format`（根目录）。挑 `extension_example.cpp`，拷贝一段「故意打乱缩进、用 tab、include 乱序」的版本到一个临时文件 `bad.cpp`。
2. 运行（需装 clang-format）：

   ```bash
   clang-format --style=file bad.cpp > good.cpp
   ```

3. `diff bad.cpp good.cpp`，观察：tab 变空格、include 重排为规范顺序、行长被截断。

**需要观察的现象**：include 顺序被重排成「本地 → UHD → 第三方 → Boost → 标准」，印证 CODING.md 的 include 规范。

**预期结果**：理解 clang-format 是 CODING.md 多数格式规则的自动执行者，贡献前必须跑。

> 若本机无 clang-format，标注「待本地验证」，改为对照 [CODING.md:62-68](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L62-L68) 的 include 示例人工核对一段代码。

#### 4.3.5 小练习与答案

**Q1**：你要提交一个「修复 `extension.cpp` 里一处逻辑 bug」的改动，顺手把附近几行的缩进也调齐了。CODING.md 会怎么看？

**答**：不鼓励。规范要求**行为修改与格式清理分开成不同 commit**（[CODING.md:149-152](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md#L149-L152)），否则 review 时格式噪音会淹没真正的逻辑变更。正确做法：先一个 cleanup commit 调格式，再一个 fix commit 改逻辑。

**Q2**：贡献一个新增的扩展（约 200 行）需要签 CLA 吗？

**答**：需要。CLA 豁免只针对「小于 10 行或文档改动」（[CONTRIBUTING.md:57-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CONTRIBUTING.md#L57-L59)）。200 行属于非平凡贡献，必须签 CLA 才能合并。而且这种规模的改动，规范建议**动手前先沟通**。

---

## 5. 综合实践

**任务**：基于 `extension_example`，写出**新增一个名为 `my_fe` 的扩展**的最小步骤清单，并指出需要在 CMake 中登记的位置。

参考答案（最小步骤清单）：

1. **建目录与接口**：仿照 `extension_example/include/`，新建 `my_fe/include/my_fe/my_fe.hpp`，定义 `class my_fe : public uhd::extension::extension`，声明独有的公共方法与 `static sptr make(factory_args)`。

2. **写实现**：新建 `my_fe/lib/my_fe.hpp`（实现类，继承接口 + `nameless_gain_mixin` + `antenna_radio_control_mixin`）和 `my_fe/lib/my_fe.cpp`：
   - 实现 `make`：从 `fargs.radio_ctrl` 取 `get_tree()`，构造实现对象。
   - 实现所有 `core_iface`/`power_reference_iface` 纯虚函数：能转发的转给 `_radio`，不支持的第抛 `not_implemented_error`。
   - 文件末尾写 `UHD_REGISTER_EXTENSION(my_fe, my_namespace::my_fe)`。

3. **写 CMake**（**需要登记的位置**）：仿照 `extension_example/CMakeLists.txt`：
   - 在 `my_fe/CMakeLists.txt` 里 `find_package(UHD 4.3.0 REQUIRED)`；
   - `add_library(my_fe SHARED ${sources})`；
   - `target_link_libraries(my_fe ${UHD_LIBRARIES} ${Boost_LIBRARIES})`；
   - **安装登记**（让 `dlopen` 能发现）：Linux 下 `install(TARGETS my_fe DESTINATION lib)` 再 `ln -s` 到 `UHD_MODULE_PATH`（[CMakeLists.txt:94-102](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/extension_example/CMakeLists.txt#L94-L102)）；或运行期设 `export UHD_MODULE_PATH=...`。

4. **编译安装**：`mkdir build && cd build && cmake .. && make && make install`。

5. **验证加载**：`uhd_usrp_probe --args "extension=my_fe"`；成功则说明注册表里有了 `my_fe`，失败会得到 `Unrecognized extension`（[multi_usrp_rfnoc.cpp:231-233](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L231-L233)）。

> CMake 中最容易被遗漏、也最关键的一步是**第 3 步的安装/软链登记**：忘了它，扩展虽编译通过却无法被 `dlopen`，`get_extension_factory` 返回 `nullptr`，加载失败。

## 6. 本讲小结

- **扩展框架**用「自注册 + 运行期 `dlopen`」让外部射频前端控制逻辑**无需改 `libuhd` 源码**就能接入：`UHD_REGISTER_EXTENSION` 在 `main` 前把工厂写进全局 `unordered_map` 注册表，`load_modules` 静态块遍历 `UHD_MODULE_PATH` 下的库触发注册。
- **基类 `uhd::extension::extension`** 同时继承 `core_iface` 和 `power_reference_iface`，这是扩展能被 `multi_usrp` 用 `dynamic_pointer_cast` 当作射频核心的前提。
- **`multi_usrp` 的挂钩点**：构造时若参数带 `extension=<名字>`，就用扩展实例替换该通道的 `rf_core`，此后所有频率/增益/带宽调用都走扩展。
- **`extension_example`** 演示了三种钩子风格：转发、改写、抛 `not_implemented_error`，并用 `experts` 框架展示了「写属性 → 自动重算」的联动。
- **构建**的关键是 `find_package(UHD)` + `add_library(... SHARED)` + 安装到模块目录，扩展本质是一个被 `dlopen` 的独立共享库。
- **贡献代码**须过 clang-format、守 include 顺序与 commit 风格、非平凡改动签 CLA，且行为修改与格式清理要分开提交。

## 7. 下一步学习建议

- **横向对比扩展与 RFNoC 块**：扩展是「主机侧拦截射频控制」，而自定义 RFNoC 块（u3-l2、u3-l6）是「FPGA 侧新增数据处理」。两者常配合使用：自定义块做实时 DSP，扩展做主机侧控制折算。建议重读 u3-l2 的 `registry` 与本讲的 `register_extension`，体会两种注册机制的异同。
- **深入 experts**：扩展示例的 `gain_expert` 只是入门。若你的扩展需要复杂的属性依赖（如「改采样率 → 重算 DSP 缩放 → 重算增益补偿」），回到 u3-l5 系统学习 `expert_factory` 的节点类型与依赖图。
- **阅读官方文档**：[host/docs/driver_usage/extension.dox](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/docs/driver_usage/extension.dox) 列出了 `multi_usrp` 当前提供的全部扩展钩子方法，是写扩展时的权威清单。
- **想贡献回上游**：先读 [CONTRIBUTING.md](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CONTRIBUTING.md) 与 [CODING.md](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CODING.md)，在 usrp-users 邮件列表沟通后再动手。
