# 命令行工具导览：find / probe / config_info

## 1. 本讲目标

学完本讲，你应该能够：

- 知道 UHD 在 `host/utils/` 下提供了哪些常用命令行工具，以及它们各自解决什么问题。
- 会用 `uhd_config_info` 查看一份「已安装 UHD 的构建指纹」（版本、编译器、启用的组件、各种路径），并逐项解释输出含义。
- 理解 `uhd_find_devices` 是如何通过 `uhd::device::find` 发现设备、又如何按序列号去重合并多个网络接口的。
- 学会用 `uhd_usrp_probe` 打开一台真实设备，把它的主板、子板、前端、RFNoC 块等能力「打印」出来，并知道它还能做属性树查询和寄存器读写。
- 在没有硬件的情况下，也能通过阅读源码列出一个工具支持的全部命令行选项。

本讲是「纯阅读型 + 可运行型」混合实践：`uhd_config_info` 不需要任何硬件就能跑，是检验你本地 UHD 是否安装正确的第一个命令。

## 2. 前置知识

本讲是 UHD 学习路线里第一次让你「真的运行 UHD」的讲义，但不需要你已经写过 UHD 程序。下面几个概念来自前序讲义，我们只做最简回顾：

- **libuhd**：UHD 的主机端共享库（`libuhd.so` / `uhd.dll`），命令行工具都是链接它编译出来的小可执行文件（见 u1-l2、u1-l3）。
- **构建指纹（build info）**：CMake 在编译时把编译器、依赖版本、启用的组件、安装前缀等「烘焙」进库里的只读元数据（见 u1-l3、u1-l4）。`uhd_config_info` 就是把这些数据读出来打印。
- **版本号四段式**：`MAJOR.API.ABI.PATCH`，例如 `4.10.0.0`（见 u1-l1、u1-l3）。
- **device_addr_t**：一个键值对结构，用来给 UHD 提供设备查找提示，例如 `"type=usrp1"` 或 `"addr=192.168.10.2"`。本讲会用到它的「字符串构造」形式，更深入的细节留给 u2-l2。
- **property_tree（属性树）**：设备内部一棵树状的配置存储。`uhd_usrp_probe` 本质上就是把这棵树的若干节点格式化打印出来（详见 u2-l4，本讲只需建立直观印象）。
- **RFNoC**：现代 USRP（N/X 系列等）的「片上射频网络」架构。`uhd_usrp_probe` 会额外尝试用 `rfnoc_graph` 接口枚举设备上的 RFNoC 块（详见第三单元）。

还需要两个本讲会顺带解释的通用知识点：

- **boost::program_options**：UHD 几乎所有命令行工具都用它来解析命令行参数。基本套路是「声明一个 `options_description`，往里 `add_options()` 一堆选项，解析后存进 `variables_map`，再用 `vm.count("xxx")` 判断某个选项有没有被传入」。
- **UHD_SAFE_MAIN**：UHD 提供的一个宏，把真正的 `main` 包一层 `try/catch`，让工具在抛异常时能优雅地打印错误并退出。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `host/utils/uhd_config_info.cpp` | 打印已安装 UHD 的版本、构建信息与各类路径，**不需要硬件**。 |
| `host/utils/uhd_find_devices.cpp` | 扫描网络/总线上可被 UHD 发现的设备，按序列号去重后打印设备地址。 |
| `host/utils/uhd_usrp_probe.cpp` | 打开指定设备，遍历属性树，格式化打印主板/子板/前端/RFNoC 能力；也支持属性树查询与寄存器交互。 |
| `host/utils/CMakeLists.txt` | 上面三个工具的构建登记处：在 `util_runtime_sources` 列表里逐个 `add_executable` 并安装。 |
| `host/include/uhd/build_info.hpp` | `uhd_config_info` 读取的构建指纹函数声明集合。 |
| `host/include/uhd/utils/safe_main.hpp` | `UHD_SAFE_MAIN` 宏的定义。 |
| `host/include/uhd/device.hpp` | `device::find` / `device::make` 的声明，是 `find_devices` 与 `usrp_probe` 的核心入口。 |
| `host/include/uhd/utils/paths.hpp` | `get_lib_path` / `get_pkg_path` 等路径查询函数，供 `config_info` 打印安装布局。 |

三个工具在 CMake 里是「兄弟」关系：它们被放进同一个列表，用同一个循环编译、链接 `uhd` 与 `Boost::program_options`，并安装到运行时路径（`${CMAKE_INSTALL_BINDIR}`）。这一点我们会在 4.1 里首先确认，因为它决定了「为什么这三个工具长得这么像」。

## 4. 核心概念与源码讲解

### 4.1 uhd_config_info：查看构建指纹与安装路径

#### 4.1.1 概念说明

`uhd_config_info` 是你装好 UHD 后**第一个该运行的命令**。它不连接任何硬件，只回答一个问题：「我电脑上这份 UHD 是怎么编译出来的、装在哪里、启用了哪些组件？」

这个问题之所以重要，是因为 UHD 是高度可配置的：编译时是否带 USB（libusb）支持、是否带 DPDK 高性能网络栈、是否带 Python 绑定、装在哪个前缀下——这些都会影响后续工具的行为。当 `uhd_find_devices` 找不到设备、或 `uhd_usrp_probe` 报某个功能不可用时，第一步永远是 `uhd_config_info --enabled-components` 看看你这份 UHD 到底编了什么。

`uhd_config_info` 打印的数据分两类来源：

1. **构建期烘焙的元数据**（版本、编译器、编译选项、Boost/libusb/DPDK 版本、启用组件、安装前缀）：由 CMake 在编译时写死进库里，对应 `uhd::build_info` 命名空间下的一组函数。这些函数都只是 `return` 一个常量字符串，没有任何运行期计算。
2. **运行期推算的路径**（库路径、包路径、包数据路径、镜像目录）：由 `uhd::get_lib_path()` 等函数根据「`libuhd` 自身在磁盘上的位置」反推出来的目录布局。也就是说，哪怕你把整个安装目录搬了家，这些路径也能跟着正确变化。

在精读源码之前，我们先记住三个工具共用的「骨架」，因为 `uhd_config_info` 是其中最简单的一个，正好拿来当模板：

> **UHD 命令行工具通用骨架**：
> 1. 用 `UHD_SAFE_MAIN(int argc, char* argv[])` 取代裸 `main`，获得顶层异常兜底。
> 2. 用 `boost::program_options` 声明一个 `options_description`，把支持的所有命令行选项登记进去。
> 3. 解析命令行存入 `variables_map vm`，用 `vm.count("选项名")` 判断每个选项是否被传入。
> 4. 通常先处理 `--help`（无参或带 `--help` 时打印帮助并以非零码退出）。
> 5. 执行工具的真正逻辑，按选项决定打印什么。

`UHD_SAFE_MAIN` 的定义很短，就是把真正的 `_main` 包进 `try/catch`：

[host/include/uhd/utils/safe_main.hpp:21-34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/safe_main.hpp#L21-L34) —— 这里用宏生成了一个真正的 `main`，它调用你写的 `_main`，捕获 `std::exception` 打印到 stderr，否则返回 `~0`（即 -1）。这样所有 UHD 工具都不必各自写异常处理。

`build_info` 命名空间则是一组「只返回编译期常量」的函数声明：

[host/include/uhd/build_info.hpp:12-46](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/build_info.hpp#L12-L46) —— 注意每个函数都标注了 `UHD_API`（导出给外部调用），且文档注释点明了它们返回的是「this build was built with」的值，也就是 CMake 在 `configure_file` 时写死的数据链（详见 u1-l3、u1-l4）。

#### 4.1.2 核心流程

`uhd_config_info` 的执行流程几乎是「骨架」的教科书实现：

```
main(argc, argv)                        # 被 UHD_SAFE_MAIN 包裹
  ├── 声明 options_description desc
  ├── desc.add_options()  登记约 18 个选项
  ├── parse_command_line + store + notify → vm
  ├── if (help 或 vm 为空)  → 打印帮助, return EXIT_FAILURE
  ├── print_all = (count("print-all") > 0)
  └── 逐个 if (count(某选项) or print_all):
          打印对应的一行信息
```

关键设计点：

- **`--print-all` 是一个「或」开关**：每个选项的判断条件都是 `vm.count("xxx") > 0 or print_all`。也就是说传 `--print-all` 等价于把所有布尔型选项一次性全部触发，而单个选项则只打印那一项。这让工具既能做全量体检，也能做精确查询。
- **无参数运行 = 打印帮助并以失败码退出**：`if (vm.count("help") > 0 or vm.empty())`。这是 UHD 工具的惯例——「不知道你要干嘛，就先看帮助」。
- **打印项与数据来源一一对应**：版本相关走 `uhd::get_version_string()`、`uhd::get_abi_string()`；构建期元数据走 `uhd::build_info::xxx()`；运行期路径走 `uhd::get_lib_path()` 等。

#### 4.1.3 源码精读

文件头部的 include 揭示了它依赖哪些公共头：

[host/utils/uhd_config_info.cpp:7-12](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L7-L12) —— `build_info.hpp`、`paths.hpp`、`version.hpp` 提供数据，`safe_main.hpp` 提供宏，`boost/program_options.hpp` 提供命令行解析。

入口和选项声明：

[host/utils/uhd_config_info.cpp:16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L16) 用 `UHD_SAFE_MAIN` 取代裸 `main`。

[host/utils/uhd_config_info.cpp:20-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L20-L41) 登记了工具支持的**全部命令行选项**。这张表本身就是「无硬件实践」的答案来源——你能从注释里读到每个选项打印什么。逐项整理如下：

| 选项 | 打印内容 | 数据来源 |
| --- | --- | --- |
| `--version` | `UHD <版本字符串>` | `uhd::get_version_string()` |
| `--build-date` | 构建日期（GMT） | `build_info::build_date()` |
| `--c-compiler` / `--cxx-compiler` | C / C++ 编译器 | `build_info::c_compiler()` / `cxx_compiler()` |
| `--c-flags` / `--cxx-flags` | C / C++ 编译选项 | `build_info::c_flags()` / `cxx_flags()` |
| `--enabled-components` | 编译期启用的组件（逗号分隔） | `build_info::enabled_components()` |
| `--install-prefix` | CMake 安装前缀 | `build_info::install_prefix()` |
| `--boost-version` | 构建时用的 Boost 版本 | `build_info::boost_version()` |
| `--dpdk-version` | 构建时用的 DPDK 版本 | `build_info::dpdk_version()` |
| `--libusb-version` | 构建时用的 libusb 版本（空则打印 `N/A`） | `build_info::libusb_version()` |
| `--lib-path` | 库所在目录 | `uhd::get_lib_path()` |
| `--pkg-path` | 包根目录（库路径的父目录） | `uhd::get_pkg_path()` |
| `--pkg-data-path` | 包数据目录（镜像、RFNoC YAML、校准数据） | `uhd::get_pkg_data_path()` |
| `--images-dir` | 镜像目录 | `uhd::get_images_dir("")` |
| `--abi-version` | ABI 版本字符串（前三段，决定 `libuhd.so` 的 SOVERSION） | `uhd::get_abi_string()` |
| `--print-all` | 触发打印以上全部 | （开关） |
| `--help` | 打印帮助 | （开关） |

帮助与「无参即帮助」逻辑：

[host/utils/uhd_config_info.cpp:48-51](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L48-L51) —— 注意这里返回的是 `EXIT_FAILURE`，所以脚本里用退出码判断「是否打印了帮助」要小心。

`--print-all` 的开关效应和版本打印：

[host/utils/uhd_config_info.cpp:53-57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L53-L57) —— `print_all` 为真时，下面每一项的 `or print_all` 都会被触发。

几个有代表性的打印分支：

- 启用组件（这是排查「为什么功能不可用」时最常用的一项）：

  [host/utils/uhd_config_info.cpp:73-76](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L73-L76)

- libusb 版本做了「空串转 `N/A`」的小处理：

  [host/utils/uhd_config_info.cpp:86-90](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L86-L90)

- 一组路径类选项，分别对应 `paths.hpp` 里的运行期路径函数：

  [host/utils/uhd_config_info.cpp:91-101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L91-L101) —— 这些函数的语义可对照 [host/include/uhd/utils/paths.hpp:29-49](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/paths.hpp#L29-L49)：`get_lib_path` 返回 `libuhd` 自身所在目录，`get_pkg_path` 是其父目录，`get_pkg_data_path` 通常等于 `pkg_path/share/uhd`，是镜像、RFNoC YAML、校准数据的存放处。

最后是 ABI 版本字符串：

[host/utils/uhd_config_info.cpp:103-105](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L103-L105) —— 这个字符串就是决定 `libuhd.so` 的 SOVERSION 的前三段版本号（如 `4.10.0`），是判断「我的程序能不能链上这份 UHD」的关键（见 u1-l3）。

这三个工具的「兄弟关系」体现在 CMake 里：

[host/utils/CMakeLists.txt:153-170](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/CMakeLists.txt#L153-L170) —— `util_runtime_sources` 列表把 `uhd_config_info.cpp`、`uhd_find_devices.cpp`、`uhd_usrp_probe.cpp` 等并列放在一起，用一个 `foreach` 循环为每个源文件 `add_executable`、链接 `uhd Boost::program_options`，并 `UHD_INSTALL` 到 `${CMAKE_INSTALL_BINDIR}`。整段逻辑只在 `ENABLE_UTILS` 为真时执行（前面有 `if(NOT ENABLE_UTILS) return()` 的早退）。这解释了为什么三个工具结构如此相似：它们就是同一个构建模板的产物。

#### 4.1.4 代码实践

**实践目标**：用 `uhd_config_info` 给本地 UHD 做一次「体检」，并逐项解释输出含义。这个实践**不需要任何 USRP 硬件**。

**操作步骤**：

1. 确认 UHD 已安装并在 `PATH` 中。运行：

   ```bash
   uhd_config_info --version
   ```

2. 运行全量打印：

   ```bash
   uhd_config_info --print-all
   ```

3. 单独查询某个关键项，例如：

   ```bash
   uhd_config_info --enabled-components
   uhd_config_info --images-dir
   uhd_config_info --abi-version
   ```

4. 故意不传任何参数，观察行为：

   ```bash
   uhd_config_info
   echo $?   # 查看退出码
   ```

**需要观察的现象与预期结果**：

- `--version` 应打印形如 `UHD 4.10.0.0-xxx` 的版本字符串。
- `--print-all` 会依次打印：版本、构建日期、C/C++ 编译器与编译选项、启用组件、安装前缀、Boost/libusb/DPDK 版本、各类路径、ABI 版本字符串。
- `--enabled-components` 输出一串逗号分隔的组件名（如包含 `USB`、`B200`、`MPMD`、`DPDK`、`Python API` 等，具体取决于你的编译配置）。若某功能不可用，多半是对应组件没出现在这里。
- `--libusb-version` 如果编译时没启用 USB，会打印 `Libusb version: N/A`（源码里对空串做了特判）。
- 无参数运行会打印帮助信息，且退出码为 **1**（`EXIT_FAILURE`），与 `--help` 行为一致。

**如果本地没装 UHD（无法运行）**：直接打开 [host/utils/uhd_config_info.cpp:20-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L20-L41)，把 `desc.add_options()` 里登记的全部选项抄成一张清单（本讲 4.1.3 的表格就是答案），并标注每个选项调用的数据来源函数。

> 说明：本实践未在你的环境实际执行，运行结果以你本地为准；如无硬件但已安装 UHD，`uhd_config_info` 部分均可正常运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `uhd_config_info` 不带任何参数也会打印帮助？退出码是多少？
**答案**：因为源码里判断条件是 `if (vm.count("help") > 0 or vm.empty())`，`vm.empty()` 表示没传入任何选项，此时也走帮助分支，并返回 `EXIT_FAILURE`（退出码 1）。

**练习 2**：`--enabled-components` 显示的「启用组件」是运行时检测的，还是编译期写死的？依据是什么？
**答案**：编译期写死的。它调用 `uhd::build_info::enabled_components()`，而 `build_info` 命名空间下的函数文档明确说返回的是「this build was built with」的值，是 CMake 在编译时通过 `configure_file` 烘焙进库的常量（见 u1-l3 的 `configure_file` 数据链）。

**练习 3**：如果你把 UHD 的整个安装目录从 `/usr/local` 搬到 `/opt/uhd`，`--lib-path` 的输出会变吗？为什么？
**答案**：会变。`--lib-path` 调用的是 `uhd::get_lib_path()`，它是根据 `libuhd` 共享库自身在磁盘上的实际位置**运行期推算**出来的，而不是编译期写死的安装前缀；所以搬家后路径会跟着变。（对比之下，`--install-prefix` 是编译期写死的，不会变。）

---

### 4.2 uhd_find_devices：发现网络与总线上的设备

#### 4.2.1 概念说明

`uhd_find_devices` 回答的问题是：「我的电脑现在能看到哪些 USRP？」它扫描所有 UHD 支持的传输通道（以太网、USB 等），列出能被发现的设备及其地址。

它的核心入口是 UHD 设备层的静态方法 `device::find`：

[host/include/uhd/device.hpp:57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L57) —— `static device_addrs_t find(const device_addr_t& hint, device_filter_t filter = ANY);`。它接受一个查找提示（hint），返回一组 `device_addr_t`（设备地址）。`hint` 用来缩小范围（比如只找某个 IP），`filter` 用来限定设备类型（默认 `ANY`，即所有类型都找）。`find` 的完整工厂机制（自注册、find/make/filter 三元组）会在 u2-l1 详讲，本讲你只需要知道「`find` 返回一批设备地址」即可。

`uhd_find_devices` 在调用 `find` 时做了一件额外的事：它在 hint 里**自动追加 `find_all=1`**。`find_all=1` 是一个特殊的提示键，告诉设备层「尽可能把同一个设备的多个可达接口（例如同时通过多个 IP 或多种传输方式）都报上来」。这样工具就能在第二步按序列号把这些「同设备多接口」合并，给用户一个干净的去重视图。

为什么需要去重？因为同一台 USRP 可能同时通过多个网络接口可达（多播发现 + 单播 IP + 子网广播……），`find` 会为每个可达路径返回一条 `device_addr_t`，它们共享同一个 `serial`（设备序列号）但其他键（如 `addr`、`type`、`product`）可能不同。直接打印会显得「设备重复了好几次」，所以工具按 `serial` 分组，把同一个序列号下的所有键值合并成一组。

#### 4.2.2 核心流程

```
main(argc, argv)
  ├── 声明 options: --help, --args (默认 "")
  ├── 解析 → vm
  ├── if (count("help"))  打印帮助, return EXIT_SUCCESS
  ├── args = device_addr_t(vm["args"])          # 字符串 → 键值对
  ├── device_addrs = device::find(append_findall(args))   # 自动加 find_all=1
  ├── if (device_addrs 为空)  打印 "No UHD Devices Found", return EXIT_FAILURE
  └── 按 serial 去重合并:
        for 每条 device_addr:
            以 serial 为分组键
            把其余键的所有取值收进 set
            把后续相同 serial 的条目擦除并合并
        最后按设备序号格式化打印
```

去重算法是「以 `serial` 为唯一身份」的 O(n²) 合并：对每条地址，向后扫描所有相同 `serial` 的地址，把它们的键值并进自己的 `set`，然后把这些重复条目从列表里删掉。对于现场设备数量（通常个位数）来说，O(n²) 完全够用。

#### 4.2.3 源码精读

文件头用一个匿名命名空间定义了 `append_findall` 辅助函数：

[host/utils/uhd_find_devices.cpp:17-25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L17-L25) —— 它复制一份 `device_addr_t`，若没有 `find_all` 键就加上 `find_all=1`，否则原样返回（不覆盖用户显式传入的值）。

入口与选项声明（注意它**只有两个**选项）：

[host/utils/uhd_find_devices.cpp:30-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L30-L37) —— `--args` 默认为空字符串，表示「不限定，全部发现」。你也可以传 `--args "addr=192.168.10.2"` 之类来缩小范围。

真正的发现调用与「找不到设备」处理：

[host/utils/uhd_find_devices.cpp:51-56](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L51-L56) —— 注意三件事：① `device_addr_t` 可以直接从字符串构造（字符串里的 `key=value` 会被解析成键值对）；② 调用前包了 `append_findall`；③ 找不到设备时打印到 **stderr**（`std::cerr`）并返回失败码——这是脚本可用的错误信号。

按 `serial` 去重合并的核心循环：

[host/utils/uhd_find_devices.cpp:61-81](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L61-L81) —— 数据结构是 `map<serial, map<key, set<value>>>`：外层按序列号分组，中层是每个键，内层用 `set` 收集该键所有出现过的不同取值。对每个 `serial`，先把自身的键塞进去，再向后扫描同序列号的条目合并、`erase` 掉重复项。`erase` 返回下一个迭代器，避免迭代器失效。

格式化打印：

[host/utils/uhd_find_devices.cpp:83-98](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L83-L98) —— 每台设备打印一条「`-- UHD Device N`」分隔块，先打印 `serial`，再遍历所有键及其所有取值。因为同一个键可能有多个取值（同一设备多个可达 IP），所以对每个键的 `set` 逐个打印。

#### 4.2.4 代码实践

**实践目标**：理解 `uhd_find_devices` 的命令行接口与去重输出格式；有硬件时实地发现一台设备，无硬件时做源码阅读型实践。

**操作步骤**：

1. 查看工具支持的选项（只有两个）：

   ```bash
   uhd_find_devices --help
   ```

2. 全量发现（不限定参数）：

   ```bash
   uhd_find_devices
   ```

3. 限定到某个设备地址（按你的网络替换 IP）：

   ```bash
   uhd_find_devices --args "addr=192.168.10.2"
   ```

**需要观察的现象与预期结果**：

- 若发现到设备，输出形如：

  ```
  --------------------------------------------------
  -- UHD Device 0
  --------------------------------------------------
  Device Address:
      serial: 3113D4F
      product: B210
      type: b200
      ...
  ```

- 若同一台设备被多个接口发现，你会看到同一个 `serial` 下出现多个不同的 `addr` 取值（这正是去重合并的效果）。
- 若没有发现任何设备，输出 `No UHD Devices Found`（在 stderr），退出码为 1。

**无硬件的源码阅读型实践**：打开 [host/utils/uhd_find_devices.cpp:34-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L34-L37)，确认工具只接受 `--help` 与 `--args` 两个选项；再阅读 4.2.3 的去重循环，回答：如果同一台设备通过 `addr=10.0.0.1` 和 `addr=10.0.0.2` 两个 IP 被发现（`serial` 相同），最终输出里 `addr` 这一行会出现几次？为什么？（答案：两次，因为它们被收进同一个 `set` 后逐个打印。）

> 说明：本实践未在你的环境实际执行；发现结果取决于你的网络与硬件。

#### 4.2.5 小练习与答案

**练习 1**：`uhd_find_devices` 为什么要自己加 `find_all=1`，而不是让用户自己传？
**答案**：因为「列出所有设备的所有可达接口」正是这个工具的用途；`find_all=1` 让设备层把同一设备的多个接口都返回，工具再按 `serial` 去重合并，给用户一个既完整又无冗余的视图。把它内置成默认行为，避免用户漏传导致发现不全。

**练习 2**：去重时为什么用 `serial` 作为分组键，而不是 `addr`？
**答案**：序列号（`serial`）是设备硬件的唯一身份标识，一台设备的所有可达接口共享同一个 `serial`；而 `addr`（IP 地址）一台设备可能有多个，不适合做「同一设备」的判据。

**练习 3**：找不到设备时，错误信息打到 stdout 还是 stderr？退出码是多少？这对脚本编写有什么意义？
**答案**：打到 `std::cerr`（stderr），退出码是 `EXIT_FAILURE`（1）。意义在于：脚本可以用退出码判断「是否发现到设备」，而不会把错误信息和正常的设备列表混在 stdout 里污染管道下游。

---

### 4.3 uhd_usrp_probe：打开设备并探查能力

#### 4.3.1 概念说明

`uhd_find_devices` 只「发现」设备（不真正打开它），而 `uhd_usrp_probe` 会**真正建立连接、打开设备**，然后把它的内部能力详尽地打印出来。它是你拿到一台陌生 USRP 后用来「摸清家底」的工具：主板型号、固件/FPGA/MPM 版本、子板与前端、频率/增益/带宽范围、天线选项、传感器列表、RFNoC 块清单……全都来源于设备内部的 `property_tree`（属性树）。

这里出现一个贯穿后续多个讲义的核心抽象——**属性树（property_tree）**。你可以把它想象成一台设备内部的「文件系统」：根是 `/`，下面有 `/name`、`/mboards/0/name`、`/mboards/0/dboards/0/rx_frontends/0/freq/range` 等节点，每个节点存着一个强类型的值（字符串、范围、传感器值……）。`uhd_usrp_probe` 本质上就是一组「访问属性树的若干路径，把值格式化成人类可读字符串」的函数。属性树的深入机制留给 u2-l4，这里你只需建立「probe = 遍历属性树并美化打印」的直觉。

除了默认的「打印设备能力」，`uhd_usrp_probe` 还隐藏了几个高级用法，都通过命令行选项触发：

- `--tree`：递归打印**整棵**属性树的所有节点路径（调试利器，能让你看到设备内部到底有哪些可访问的键）。
- `--string` / `--double` / `--int` / `--range` / `--sensor`：精确查询单个属性的值（分别按字符串、浮点、整数、范围、传感器值解析）。
- `--vector`：配合 `--string`，把字符串当 `std::vector<std::string>` 解析。
- `--init-only`：只初始化设备，跳过所有查询（用于测试设备能否被打开）。
- `--interactive-reg-iface`：对 RFNoC 设备，进入一个交互式 shell，可以 `peek32`/`poke32` 读写指定块的寄存器。

#### 4.3.2 核心流程

`uhd_usrp_probe` 的默认执行流程（不带查询类选项时）：

```
main(argc, argv)
  ├── 声明约 12 个 options
  ├── 解析 → vm
  ├── if (count("help"))  打印帮助, return EXIT_FAILURE
  ├── if (count("version"))  打印版本, return EXIT_SUCCESS
  ├── dev   = device::make(args)            # 真正打开设备
  ├── tree  = dev->get_tree()               # 拿到属性树
  ├── 尝试 graph = rfnoc_graph::make(args)  # RFNoC 设备才成功
  │     └── 若抛 key_error，说明非 RFNoC 设备，graph 保持空
  ├── 若带 --string/--double/--int/--sensor/--range:
  │     └── 查询单个属性后 return
  ├── 若带 --interactive-reg-iface:
  │     └── 进入寄存器交互 shell 后 return
  └── 默认分支:
        if (--tree)  print_tree("/", tree)            # 递归打印整棵树
        else (非 --init-only):
            pp = get_device_pp_string(tree)           # 主板/子板/前端/codec
            if (graph)  pp += get_rfnoc_pp_string(...) # 追加 RFNoC 块信息
            打印美化后的 pp
```

几个关键设计：

- **设备打开是「真连」**：`device::make(args)` 会真正建立到设备的传输通道，因此这一步要求设备在线且可访问。与 `find` 不同，`make` 返回的是一个可操作的设备对象。
- **RFNoC 探测是「尽力而为」**：代码用 `try/catch (uhd::key_error)` 包住 `rfnoc_graph::make`。对于非 RFNoC 设备（老型号），这一步会抛 `key_error`，被捕获后 `graph` 保持为空，工具就只打印传统的主板/子板信息；对 RFNoC 设备则额外打印块清单与静态连接。这种「优雅降级」让一个工具同时服务新老两类设备。
- **`get_device_pp_string` 是分层格式化**：`设备 → 主板 → DSP/子板 → 前端/codec`，每一层用一个 `make_border` 包出 ASCII 边框，形成经典的「竖线框」输出。

#### 4.3.3 源码精读

文件头部 include 了很多头，可见它访问的设备面非常广：

[host/utils/uhd_usrp_probe.cpp:8-27](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L8-L27) —— `property_tree.hpp`（遍历树）、`rfnoc_graph.hpp` 与 `rfnoc/block_id.hpp`（RFNoC）、`types/ranges.hpp` 与 `types/sensors.hpp`（频率/增益范围、传感器值）、`usrp/dboard_eeprom.hpp`/`mboard_eeprom.hpp`（子板/主板 EEPROM）、`cast.hpp`（交互式 shell 的整数解析）。

入口与全部选项：

[host/utils/uhd_usrp_probe.cpp:405-423](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L405-L423) —— 这是「列出该工具全部命令行选项」的权威位置。注意 `--args`、`--string`、`--double` 等带值，`--tree`、`--vector`、`--init-only` 是开关。

打开设备、取属性树、尝试 RFNoC：

[host/utils/uhd_usrp_probe.cpp:440-447](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L440-L447) —— `device::make` 接受字符串参数（会自动解析成 `device_addr_t`）；`get_tree()` 拿到属性树句柄；`rfnoc_graph::make` 用 `try/catch (uhd::key_error)` 包住，非 RFNoC 设备时 `graph` 保持空。

精确查询类选项的代表——`--range`（查询一个范围并按 `start:step:stop` 格式打印）：

[host/utils/uhd_usrp_probe.cpp:485-492](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L485-L492) —— 它把属性树里某个 `meta_range_t` 节点取出来，按 `start:step:stop` 打印。`--string`/`--double`/`--int`/`--sensor` 的结构完全一样，只是模板参数换成对应类型。每个查询分支末尾都 `return EXIT_SUCCESS`，所以「查询类选项」与「默认打印」是互斥的。

默认分支：`--tree` 递归打印整棵树，否则格式化打印设备能力（RFNoC 设备再追加块信息）：

[host/utils/uhd_usrp_probe.cpp:506-514](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L506-L514) —— `get_device_pp_string(tree)` 产出主板/子板/前端/codec 的美化串；若 `graph` 非空，再 `get_rfnoc_pp_string(graph, tree)` 追加 RFNoC 信息；最后统一用 `make_border` 包边框打印。

整棵树递归打印的实现非常短，但极具教学意义——它揭示了属性树的「目录式」结构：

[host/utils/uhd_usrp_probe.cpp:328-334](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L328-L334) —— `print_tree(path, tree)`：打印当前路径，再用 `tree->list(path)` 列出所有子节点，对每个子节点递归。这与遍历文件系统目录的逻辑完全同构。

主板层的格式化函数最能体现「probe = 遍历属性树」的本质：

[host/utils/uhd_usrp_probe.cpp:231-314](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L231-L314) —— `get_mboard_pp_string` 依次访问 `name`、`eeprom`（主板 EEPROM，含序列号等）、`fw_version`/`mpm_version`/`fpga_version`/`fpga_version_hash`、`device_dna`，以及 `/blocks` 是否存在（判断是否 RFNoC capable）。随后在 `try` 块里访问 `time_source/options`、`clock_source/options`、`sensors`、`rx_dsps`、`dboards`、`tx_dsps`，把时间源/时钟源/传感器/DSP/子板都打印出来；用 `catch (const uhd::lookup_error&)` 兜底，某个节点缺失时优雅退出该段而不崩溃。

RFNoC 块与静态连接的枚举：

[host/utils/uhd_usrp_probe.cpp:195-213](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L195-L213) —— `get_rfnoc_blocks_pp_string` 用 `graph->find_blocks("")`（空串表示「全部」）枚举设备上所有 RFNoC 块；`get_rfnoc_connections_pp_string` 用 `graph->enumerate_static_connections()` 列出 FPGA 里预先连好的静态边。这两个接口是第三单元 RFNoC 内容的预告。

前端层的格式化函数展示了「如何从属性树读出射频能力」：

[host/utils/uhd_usrp_probe.cpp:74-126](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L74-L126) —— `get_frontend_pp_string` 依次读出前端的 `name`、`antenna/options`（天线选项）、`sensors`（传感器列表）、`freq/range`（频率范围）、`gains/*/range`（各增益元件的范围）、`bandwidth/range`（带宽范围）、`connection`（前端连接类型）、`use_lo_offset`（是否使用 LO 偏移）。这正是你用 `uhd_usrp_probe` 看到的那一屏「Frontend」信息的来源。

最后看一个高级功能——交互式寄存器 shell：

[host/utils/uhd_usrp_probe.cpp:494-504](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L494-L504) —— `--interactive-reg-iface` 要求必须是 RFNoC 设备（`graph` 非空），否则报错退出；它按 `block_id` 取出块控制器，进入 `run_interactive_regs_shell`，可 `poke32 $addr $data` 写寄存器、`peek32 $addr` 读寄存器。这是底层调试时直接操作 RFNoC 块寄存器的入口。

#### 4.3.4 代码实践

**实践目标**：掌握 `uhd_usrp_probe` 的几种用法模式，理解其输出如何映射到属性树。有硬件时实地探查一台设备；无硬件时做源码阅读与流程梳理。

**操作步骤**：

1. 查看全部选项（这是无硬件实践的核心）：

   ```bash
   uhd_usrp_probe --help
   ```

2. 默认探查（需要一台在线 USRP，按你的地址替换 `args`）：

   ```bash
   uhd_usrp_probe --args "addr=192.168.10.2"
   ```

3. 打印整棵属性树（强烈推荐的调试手段）：

   ```bash
   uhd_usrp_probe --args "addr=192.168.10.2" --tree
   ```

4. 精确查询单个属性（示例路径，实际路径以 `--tree` 输出为准）：

   ```bash
   uhd_usrp_probe --args "type=b200" --range "/mboards/0/dboards/0/rx_frontends/0/freq/range"
   ```

5. 只测试设备能否被打开，不打印能力：

   ```bash
   uhd_usrp_probe --args "addr=192.168.10.2" --init-only
   ```

**需要观察的现象与预期结果**：

- 默认输出是一组用竖线边框包裹的段落：先是 `Mboard: <主板名>` 与各种版本号（FW/MPM/FPGA）、`Device DNA`、`RFNoC capable: Yes/No`，然后是各 RX/TX DSP 的频率范围、各子板与前端的天线/频率/增益/带宽范围、传感器列表。RFNoC 设备还会列出 `RFNoC blocks on this device:` 与 `Static connections on this device:`。
- `--tree` 会以缩进形式打印出成百上千行路径，让你看到设备内部几乎所有可访问的键。
- `--range` 会以 `start:step:stop` 格式打印一个范围。
- `--init-only` 几乎不输出，仅用于「能否成功 `make` 设备」的健康检查。

**无硬件的源码阅读型实践**：对照 [host/utils/uhd_usrp_probe.cpp:506-514](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L506-L514) 的默认分支，画出工具的决策流程图：帮助 → 版本 → 打开设备/取树/尝试 RFNoC → 各查询选项 → 交互式 shell → `--tree` 或默认打印。再打开 [host/utils/uhd_usrp_probe.cpp:231-314](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L231-L314)，数一数 `get_mboard_pp_string` 访问了多少个属性树路径，把它们整理成一张「主板层属性路径表」。

> 说明：本实践未在你的环境实际执行；探查结果取决于具体设备型号与版本。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `uhd_usrp_probe` 用 `try/catch (uhd::key_error)` 包住 `rfnoc_graph::make`？如果去掉会怎样？
**答案**：因为该工具要同时支持 RFNoC 设备（N/X 系列等）和老式非 RFNoC 设备。对非 RFNoC 设备，`rfnoc_graph::make` 会抛 `key_error`；包住后 `graph` 保持为空，工具就只打印传统主板/子板信息，实现「优雅降级」。若去掉 try/catch，遇到非 RFNoC 设备时异常会一路抛到 `UHD_SAFE_MAIN` 的兜底 catch，工具直接报错退出，连基本的主板信息都打印不了。

**练习 2**：`--tree` 选项打印的内容和默认打印有什么本质区别？
**答案**：`--tree` 调用 `print_tree("/", tree)`，递归列出整棵属性树的**所有节点路径**（键名），但不解释值；默认打印则只访问一组**精心挑选的节点**，并把它们的值格式化成人类可读的能力描述（频率范围、增益、天线等）。前者是「看到设备内部结构」，后者是「看到设备能力摘要」。

**练习 3**：`uhd_find_devices` 和 `uhd_usrp_probe` 都会「接触」设备，二者最关键的区别是什么？
**答案**：`find_devices` 调用的是 `device::find`，只做**发现**（列出地址，不真正打开设备、不建立持续的数据通道）；`usrp_probe` 调用的是 `device::make`，会**真正打开设备**并取回可操作对象与属性树，因此能读出固件版本、子板能力等需要「连进去」才能拿到信息。`find` 是轻量侦察，`make` 是正式连接。

---

## 5. 综合实践

**任务**：在没有硬件的前提下，仅凭源码，编写一份《UHD 命令行工具速查表》，并在有硬件时用它完成一次完整的「设备接入体检」。

**步骤**：

1. **速查表（源码阅读型，无需硬件）**：分别为三个工具建表，每表包含「选项名 / 作用 / 数据来源或调用的 UHD 接口 / 是否需要硬件」四列。
   - `uhd_config_info` 的选项来源：[host/utils/uhd_config_info.cpp:20-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L20-L41)。
   - `uhd_find_devices` 的选项来源：[host/utils/uhd_find_devices.cpp:34-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_find_devices.cpp#L34-L37)。
   - `uhd_usrp_probe` 的选项来源：[host/utils/uhd_usrp_probe.cpp:405-423](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L405-L423)。

2. **体检流程（需硬件时执行）**，按顺序运行并记录每步输出：
   - `uhd_config_info --print-all` —— 确认 UHD 构建指纹与路径。
   - `uhd_find_devices` —— 发现设备，记录 `serial`、`type`、`product`。
   - `uhd_usrp_probe --args "<上面发现的地址>"` —— 探查能力，记录主板版本、前端频率/增益范围、是否 `RFNoC capable`。
   - `uhd_usrp_probe --args "<地址>" --tree > tree.txt` —— 导出整棵属性树备用（后续 u2-l4 会用到）。

3. **对照分析**：把第 2 步 `usrp_probe` 默认输出里看到的每个能力项（如 `Freq range`、`Gain range`），反向追溯到 `get_frontend_pp_string` 访问的属性树路径（[host/utils/uhd_usrp_probe.cpp:74-126](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L74-L126)），验证「屏幕上的每一行都对应属性树里的一个节点」。

**预期成果**：一份可复用的速查表，以及一份证明你理解「工具输出 ↔ 属性树节点 ↔ 源码函数」三者对应关系的分析记录。

## 6. 本讲小结

- UHD 的命令行工具都住在 `host/utils/`，共享同一套骨架：`UHD_SAFE_MAIN` 宏做异常兜底 + `boost::program_options` 解析选项，并在 CMake 的同一个循环里编译安装（[host/utils/CMakeLists.txt:153-170](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/CMakeLists.txt#L153-L170)）。
- `uhd_config_info` **不需要硬件**，打印的是「已安装 UHD」的构建指纹（编译期常量，来自 `build_info` 命名空间）与运行期推算的安装路径；`--print-all` 一次性触发所有打印项。
- `uhd_find_devices` 通过 `device::find(append_findall(args))` 发现设备（自动加 `find_all=1`），再按 `serial` 把同一设备的多个可达接口去重合并，找不到设备时走 stderr 并返回失败码。
- `uhd_usrp_probe` 用 `device::make` **真正打开设备**，取回属性树后格式化打印主板/子板/前端/codec 能力；对 RFNoC 设备额外枚举块与静态连接（用 `try/catch key_error` 优雅降级）。
- 除了默认打印，`uhd_usrp_probe` 还支持 `--tree`（打印整棵属性树）、`--string/--double/--int/--range/--sensor`（精确查询）、`--init-only`（仅测试可打开）、`--interactive-reg-iface`（RFNoC 寄存器读写 shell）。
- 三个工具形成自然的递进：`config_info`（自检软件）→ `find_devices`（轻量发现设备）→ `usrp_probe`（深度探查设备能力），这也是你排查任何 UHD 问题时的标准排查顺序。

## 7. 下一步学习建议

下一讲 **u1-l6 第一个示例：rx_samples_to_file** 会带你读完一个真正「收数据」的 UHD 程序骨架，把本讲建立的「工具视角」推进到「程序视角」。

在进入第二单元前，建议你带着本讲留下的几个「钩子」继续阅读：

- 本讲反复出现的 `device::find` / `device::make` 的工厂机制，详见 u2-l1（设备发现与工厂模式）。
- `device_addr_t` 键值对地址与 `device_filter_t` 的细节，详见 u2-l2。
- `property_tree`（属性树）的内部结构与访问方式，详见 u2-l4；本讲导出的 `--tree` 输出届时会成为最好的练习素材。
- `rfnoc_graph`、`find_blocks`、`enumerate_static_connections` 等 RFNoC 接口，详见第三单元（u3-l1 起）。
