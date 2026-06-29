# 第一个示例：rx_samples_to_file

## 1. 本讲目标

本讲是「入门篇」的最后一讲，目标是用一个真实可编译的 UHD 示例程序，把前面几讲建立的零散概念（公共头文件、命令行工具、`multi_usrp`）串成一条完整的调用链。

学完本讲你应该能够：

- 看懂任意一个 UHD 示例程序的「骨架结构」，知道它由哪几个固定阶段组成。
- 理解 `UHD_SAFE_MAIN` 宏做了什么、为什么几乎所有 UHD 程序都用它。
- 理解 `boost::program_options` 如何把命令行字符串解析成 C++ 变量。
- 手动跟踪 `rx_samples_to_file` 从「敲下命令」到「数据落盘」的完整流程，标出三个关键阶段。

本讲**只读不写**：我们不改源码，只对照源码画流程图。`multi_usrp` 的 API 细节会在进阶篇 **u2-l3** 专门讲解，这里只需知道「它是个设备对象」即可。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均来自前面几讲）：

- **公共头文件布局**（u1-l4）：UHD 的 C++ 头文件没有「伞式头文件」，需要哪个就 `#include` 哪个。本例会 include `multi_usrp.hpp`、`convert.hpp`、`tune_request.hpp` 等。
- **命令行工具骨架**（u1-l5）：`uhd_config_info`、`uhd_find_devices`、`uhd_usrp_probe` 三个工具都用了同一个套路 —— `UHD_SAFE_MAIN` + `boost::program_options`。本讲的示例程序**用的就是这个套路**。
- **设备发现与构造**（u1-l5 提到 `device::find` / `device::make`）：示例程序用更高层的 `multi_usrp::make(args)` 一步完成「发现 + 打开设备」。

两个可能陌生但本讲会用到的 C++/Boost 知识，先给一句话解释：

- **模板函数（template function）**：示例里有个 `recv_to_file<short>`，尖括号里的 `short` 表示「这次接收的样本类型是 16 位整数」。同一个函数能复用于 `double`/`float`/`short` 三种类型。
- **`std::thread`**：C++ 标准库的线程。示例默认单线程接收；加上 `--multi-streamer` 后，每个通道开一个线程。

## 3. 本讲源码地图

本讲几乎只围绕**一个源码文件**展开，外加一个宏定义头文件作为补充。

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [host/examples/rx_samples_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp) | 接收射频信号并写入文件的完整示例 | 全文：骨架、参数解析、设备构造、配置、接收循环 |
| [host/include/uhd/utils/safe_main.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/safe_main.hpp) | `UHD_SAFE_MAIN` 宏的定义 | 异常兜底机制 |
| [host/examples/CMakeLists.txt](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/CMakeLists.txt) | 示例程序的构建规则 | 本例如何被编译、链接哪些库 |

> 提示：本讲的永久链接全部指向当前 HEAD `2af4ddb96`。如果你在自己的机器上 checkout 了不同版本，行号可能略有出入，请以本地源码为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，顺序与「程序执行时的阅读顺序」一致：

1. **4.1 safe_main** —— 整个程序的最外层包装（最先被执行）。
2. **4.2 boost::program_options** —— `main` 体内第一件事：解析命令行。
3. **4.3 示例主流程** —— 解析完成后，构造设备 → 配置 → 接收 → 写文件。

### 4.1 safe_main：给 main 套一个异常兜底

#### 4.1.1 概念说明

UHD 的 C++ API 大量使用**异常**（`throw`）来报告错误：找不到设备、采样率非法、传感器锁定超时等，都会抛出 `uhd::exception` 的子类或 `std::exception` 的子类。

如果这些异常**没被捕获**，就会一路冒泡到 `main` 函数之外，C++ 运行时会调用 `std::terminate()`，程序直接崩溃，用户只看到一句干巴巴的 `Aborted (core dumped)`，完全不知道错在哪。

`UHD_SAFE_MAIN` 宏的作用就是：**在真正的 `main` 外面套一层 `try/catch`，把任何未捕获的异常转成一句可读的错误信息打印到 stderr，并返回一个失败退出码。** 这样用户至少能看到 `Error: ...` 的提示。

#### 4.1.2 核心流程

展开 `UHD_SAFE_MAIN(int argc, char* argv[])` 后，程序的入口结构是：

```
真正的 main(argc, argv)              ← 操作系统调用这里
└── try {
        return _main(argc, argv);    ← 你写的代码其实在这里
    }
    catch (const std::exception& e) {
        打印 "Error: " + e.what();
    }
    catch (...) {
        打印 "Error: unknown exception";
    }
    return ~0;                       ← 失败退出码
```

也就是说：**你写的函数体其实是 `_main`，而不是 `main`。** 宏帮你生成了真正的 `main`，并在里面调用了你的 `_main`。

#### 4.1.3 源码精读

宏定义本身非常短：

[host/include/uhd/utils/safe_main.hpp:21-34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/safe_main.hpp#L21-L34) —— 这段定义了 `UHD_SAFE_MAIN` 宏，它声明并实现了真正的 `main`，在其中 `try { return _main(argc, argv); }`，并用两层 `catch` 捕获异常。

注意几个细节：

- `~0` 是「按位取反 0」，对 `int` 而言等于 `-1`。UHD 用 `-1` 作为失败退出码（成功时用 `EXIT_SUCCESS`，即 `0`）。
- `catch (...)` 是「捕获所有异常」的兜底，处理那些不是 `std::exception` 子类的异常（例如某些第三方库直接 `throw 42;`）。

在示例文件中，主函数就这样声明：

[host/examples/rx_samples_to_file.cpp:370-371](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L370-L371) —— 示例用 `int UHD_SAFE_MAIN(int argc, char* argv[])` 声明主函数；这意味着后面整个函数体其实是 `_main`，被宏生成的 `main` 包在 `try/catch` 里调用。

> 与 u1-l5 的呼应：你在 `uhd_find_devices`、`uhd_usrp_probe` 里已经见过同一个宏。UHD 所有官方命令行程序和示例都用它，是「UHD 程序的第一行惯例」。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `UHD_SAFE_MAIN` 的异常兜底行为。

**操作步骤**（不修改项目源码，自己写一个最小测试文件，例如放到 `/tmp`，避免污染仓库）：

1. 在 `/tmp/safe_main_demo.cpp` 写下面这段**示例代码**（非项目原有代码）：

   ```cpp
   #include <uhd/utils/safe_main.hpp>
   #include <stdexcept>

   int UHD_SAFE_MAIN(int /*argc*/, char* /*argv*/[])
   {
       throw std::runtime_error("故意抛出的异常，用来测试兜底");
       return 0; // 永远到不了这里
   }
   ```

2. 用你构建 UHD 时用的编译器编译它（需要能找到 UHD 头文件，例如 `-I<uhd 源码>/host/include` 并链接 `uhd`）。

3. 运行可执行文件，观察输出。

**需要观察的现象**：

- 程序**没有**崩溃成 `Aborted (core dumped)`。
- stderr 打印了 `Error: 故意抛出的异常，用来测试兜底`。
- 用 `echo $?` 查看退出码，应为 `255`（即 `~0` 的低 8 位，`-1` 截断后是 255）。

**预期结果**：把 `throw` 那行注释掉重新编译运行，程序正常返回，`echo $?` 输出 `0`。这说明宏确实把异常转换成了「可读错误 + 非零退出码」。

**待本地验证**：如果没有可用的 UHD 编译环境，可以只阅读宏定义并在纸上展开 `UHD_SAFE_MAIN(int, char*[])`，确认你能画出 4.1.2 的流程图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `UHD_SAFE_MAIN` 要分 `catch (const std::exception& e)` 和 `catch (...)` 两层？只用一层 `catch (...)` 行不行？

**参考答案**：只用 `catch (...)` 能捕获所有异常，但拿不到错误描述（`e.what()`），只能打印一句笼统的 `unknown exception`。第一层用 `std::exception&` 可以调用 `e.what()` 拿到具体原因（例如「`No devices found for ----->`」），给出更有用的错误信息；第二层 `catch (...)` 才是用来兜那些不是 `std::exception` 子类的「漏网之鱼」。

**练习 2**：示例程序成功结束时返回的是什么？失败（如找不到设备）时呢？

**参考答案**：成功返回 `EXIT_SUCCESS`（即 `0`），见文件末尾 `return EXIT_SUCCESS;`；宏捕获到异常时返回 `~0`（即 `-1`，进程退出码通常表现为 255）。`--help` 分支也返回 `~0`（见 4.2.3），这是该示例的一个小历史习惯。

---

### 4.2 boost::program_options：把命令行字符串解析成 C++ 变量

#### 4.2.1 概念说明

`rx_samples_to_file` 有二十多个命令行选项，例如：

```
rx_samples_to_file --args "addr=192.168.10.2" --freq 2.4e09 --rate 7.68e06 --duration 0.01
```

如果用 `argc`/`argv` 手写解析，要处理「长短选项（`-f` 和 `--freq`）」「带值 vs 不带值（`--gain 30` vs `--stats`）」「默认值」「类型转换（字符串 → double）」「帮助文本」……代码会非常啰嗦且易错。

`boost::program_options`（本例里取别名 `namespace po = boost::program_options;`）是一个声明式的命令行解析库：你**一次性声明**「有哪些选项、各自的类型和默认值」，库就替你完成解析、类型转换和帮助文本生成。

#### 4.2.2 核心流程

`program_options` 的标准用法是「四步走」：

```
1. 声明变量           →  std::string file; double rate; size_t spb; ...
2. 描述选项           →  desc.add_options()("file", po::value(&file)->default_value("usrp_samples.dat"), "说明文字") ...
3. 解析并写入变量映射  →  po::store(po::parse_command_line(argc, argv, ...), vm);
4. 触发 notify         →  po::notify(vm);   ← 把解析结果真正写进第 1 步的变量
```

关键数据结构：

- **`po::options_description`**：一张「选项表」，登记每个选项的名字、类型、默认值、帮助文字。
- **`po::variables_map vm`**：解析后的「变量映射」，本质是一个 `选项名 → 值` 的字典。`vm.count("gain")` 返回某选项是否被用户提供过（`0` 或 `1`）。

#### 4.2.3 源码精读

先看取别名和变量声明：

[host/examples/rx_samples_to_file.cpp:39](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L39) —— `namespace po = boost::program_options;` 给库取短名 `po`，后续所有类型都用 `po::` 前缀引用。

[host/examples/rx_samples_to_file.cpp:442-449](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L442-L449) —— 声明 `args`、`file`、`type`、`rate`、`freq`、`gain` 等一批将被命令行填写的变量，注释 `// variables to be set by po` 点明了它们是「待 program_options 填充」的目标。

然后是选项表的声明，挑几条典型的看：

[host/examples/rx_samples_to_file.cpp:455-467](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L455-L467) —— 声明 `help`、`args` 两个选项。`("help,h", ...)` 同时支持 `--help` 和 `-h` 两种写法；`po::value<std::string>(&args)->default_value("")` 表示「这个选项需要一个字符串值，并直接写入变量 `args`，没给就用空串」。

[host/examples/rx_samples_to_file.cpp:483-491](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L483-L491) —— 声明 `spb`（每通道主机缓冲样本数）和 `rate`（采样率）。注意 `spb` 是 `size_t`、`rate` 是 `double`，库会自动把命令行字符串转成对应类型。

带默认值的「数值选项」和**不带值的「开关选项」**要区分开：

[host/examples/rx_samples_to_file.cpp:527-532](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L527-L532) —— `progress`、`stats`、`sizemap`、`null` 等选项**没有 `po::value(...)`**，它们是「布尔开关」：用户写了 `--stats` 就算「出现」，不写就没有。后面用 `vm.count("stats") > 0` 来判断。

解析与触发的两行：

[host/examples/rx_samples_to_file.cpp:548-556](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L548-L556) —— `po::store(po::parse_command_line(argc, argv, all_options), vm)` 解析命令行；若带了 `--help` 就打印帮助并 `return ~0;` 退出；**否则**才调用 `po::notify(vm)` 把结果写进变量。注意 `notify` 必须在 `--help` 检查之后调用，否则用户传了非法值时 `notify` 会先抛异常，导致看不到帮助。

> 「带值」vs「不带值」是一个高频考点：带 `po::value(...)` 的选项在命令行里要跟一个值（`--gain 30`），不带的是开关（`--stats`）。后续代码一律用 `vm.count("选项名")` 判断是否出现。

#### 4.2.4 代码实践

**实践目标**：跑一遍 `--help`，对照源码确认你能在选项表里找到每一个打印出来的选项。

**操作步骤**（无硬件也可做，只需已安装/已编译的 UHD）：

1. 运行：`rx_samples_to_file --help`
2. 把输出的「Allowed options」完整保存下来。
3. 对照 [4.2.3 的选项表源码](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L455-L541)，逐行核对：输出里的每个选项，是否都能在源码里找到对应的 `("名字", ...)` 声明？

**需要观察的现象**：

- `--help` 输出顶部的 `program_doc`（第 373-440 行那段长字符串）会先打印，包含使用示例。
- 下面跟着 `Allowed options:` 列表，列出全部选项及默认值。

**预期结果**：你能发现输出里**没有** `wirefmt` 和 `multi_streamer` 这两个选项（因为它们登记在 `alias_options` 里、帮助文本为空，是向后兼容的别名）。这正是第 542-545 行「别名表」的作用。

**待本地验证**：如果暂时没有可执行文件，可以直接阅读 [第 373-440 行的 program_doc](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L373-L440) 了解官方用法示例。

#### 4.2.5 小练习与答案

**练习 1**：示例里 `--gain` 没有设置 `default_value(...)`，而 `--rate` 设置了 `default_value(1e6)`。这两种声明方式在后续代码里有什么行为差异？

**参考答案**：有 `default_value` 的选项（如 `rate`）即使用户不提供，`vm` 里也会有值并写入变量；后续代码可以直接用 `rate`。没有 `default_value` 的选项（如 `gain`）如果用户不提供，`vm.count("gain")` 就是 `0`，必须用 `if (vm.count("gain"))` 先判断再使用，否则读取会失败。源码里频率、增益、带宽都用了 `vm.count(...)` 守卫（见 4.3）。

**练习 2**：为什么 `po::notify(vm)` 要写在 `if (vm.count("help")) { ... return; }` **之后**，而不是紧跟 `po::store` 之后？

**参考答案**：`notify` 会触发值的校验和写入，若用户传了非法值（例如把字符串塞给数值选项），`notify` 会抛异常。如果 `notify` 在 `--help` 检查之前，那么「带错误参数的同时请求帮助」就会先抛异常、用户看不到帮助。放在后面，则 `--help` 能优先生效退出。同时这也意味着——用户没给 `--help` 时，传了非法值才会被 `notify` 拦下报错。

---

### 4.3 示例主流程：从构造设备到接收写文件

#### 4.3.1 概念说明

参数解析完之后，程序进入「真正的 UHD 工作流程」。一个典型的 UHD 接收程序可以归纳成**四个阶段**：

1. **构造设备**：把设备地址字符串交给 `multi_usrp::make`，得到一个高层设备对象。
2. **配置射频参数**：设置采样率、中心频率、增益、带宽、天线、子设备映射等。
3. **接收并写文件**：创建接收流器（rx_streamer），下发流命令启动接收，在循环里 `recv` 并写盘。
4. **多线程与清理**：可选地为每个通道开一个线程；主线程等待、汇报带宽，最后 join 所有线程。

阶段 1-2 在 `UHD_SAFE_MAIN` 函数体里直接写；阶段 3 被封装进模板函数 `recv_to_file`；阶段 4 用 `std::thread` 调度。

> 关于 `multi_usrp`：它是 UHD 提供的「高层易用 API」，内部封装了设备发现（u2-l1）、属性树（u2-l4）等机制。本讲把它当成「一个能配置射频、能收发数据的黑盒」，详细 API 在 u2-l3 展开。

#### 4.3.2 核心流程

把整个程序的调用链画成流程图（虚线框表示「多通道时循环」）：

```
UHD_SAFE_MAIN  (异常兜底)
   │
   ├─ [阶段0] program_options 解析命令行  ──→ 填充 args/rate/freq/gain/...
   │
   ├─ [阶段1] 构造设备
   │     multi_usrp::make(args)   → usrp 对象
   │     set_clock_source(ref)    （可选，锁参考时钟）
   │     set_rx_subdev_spec(subdev) （可选，选子设备映射）
   │
   ├─ [阶段2] 配置射频参数
   │     set_rx_rate(rate, ALL_CHANS)        ← 采样率
   │     set_rx_freq(tune_request, chan)     ← 中心频率（每通道循环）
   │     set_rx_gain(gain, ALL_CHANS)        ← 增益（可选）
   │     set_rx_bandwidth(bw, chan)          ← 模拟带宽（可选）
   │     set_rx_antenna(ant, chan)           ← 天线（可选）
   │     sleep(setup_time)  →  check_locked_sensor("lo_locked")
   │
   ├─ [阶段3] 接收并写文件  （在 recv_to_file 模板里）
   │     get_rx_stream(stream_args)   → rx_stream
   │     分配缓冲 buffs[][]，打开输出文件
   │     issue_stream_cmd(stream_cmd)         ← 启动接收
   │     while (未达停止条件):
   │         rx_stream->recv(buffs, ..., md)   ← 收一批样本
   │         处理 md 里的 timeout / overflow / error
   │         outfiles[ch].write(...)           ← 写盘
   │     issue_stream_cmd(STOP_CONTINUOUS)     ← 停止
   │     关文件、释放缓冲
   │
   └─ [阶段4] 多线程与清理
         主线程 sleep + 周期打印带宽 Msps
         join 所有接收线程
         打印 "Done!"
```

三个**带停止条件的 `recv` 循环**控制着程序何时退出（见 4.3.3 的循环条件）。

#### 4.3.3 源码精读

**阶段 1：构造设备**

[host/examples/rx_samples_to_file.cpp:569-600](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L569-L600) —— 打印设备构造提示后，调用 `uhd::usrp::multi_usrp::make(args)` 创建高层设备对象（`sptr` 是 shared pointer）。`args` 就是 `--args` 的内容，如 `addr=192.168.10.2`。随后解析 `--channels` 字符串、（可选）锁参考时钟 `set_clock_source(ref)`、（可选）设置子设备映射 `set_rx_subdev_spec(subdev)`，并用 `get_pp_string()` 打印设备的人类可读描述。

> 顺序很重要：源码注释明确写了「**always select the subdevice first, the channel mapping affects the other settings**」（先选子设备，因为通道映射会影响后续设置）。

**阶段 2：配置射频参数（采样率、频率、增益、带宽、天线）**

[host/examples/rx_samples_to_file.cpp:602-612](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L602-L612) —— 采样率设置：先校验 `rate > 0`，然后 `usrp->set_rx_rate(rate, multi_usrp::ALL_CHANS)`（`ALL_CHANS` 表示应用到所有通道），再用 `get_rx_rate(...)` 打印「实际采样率」。注意示例反复强调：**设备可能把请求的速率四舍五入到最接近的支持值**，所以总是要回读实际值。

[host/examples/rx_samples_to_file.cpp:614-629](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L614-L629) —— 频率设置：构造 `uhd::tune_request_t(freq, lo_offset)`（含本振偏移），（可选）带 `mode_n=integer` 走整数 N 分频，对每个通道 `set_rx_freq`。增益、带宽、天线的代码结构完全平行，都是「带 `vm.count(...)` 守卫 → set → 回读实际值」，可参照 [第 631-656 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L631-L656)。

配置完射频后，程序会等待硬件稳定并检查 LO 锁定：

[host/examples/rx_samples_to_file.cpp:658-690](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L658-L690) —— `sleep(setup_time)` 等待硬件就绪；若未加 `--skip-lo`，则调用 `check_locked_sensor` 轮询 `lo_locked` 传感器（以及 `mimo_locked` / `ref_locked`），确保本振稳定后再开始接收。`check_locked_sensor` 的实现在 [第 329-368 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L329-L368)，它通过传入的 lambda 读取传感器并轮询直到锁定或超时。

**阶段 3：接收并写文件（`recv_to_file` 模板函数）**

这是示例的核心。先看流器创建与缓冲分配：

[host/examples/rx_samples_to_file.cpp:163-182](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L163-L182) —— 用 `stream_args_t(cpu_format, wire_format)` 描述「CPU 端格式」与「线上（over-the-wire）格式」，设好 `channels`，调 `usrp->get_rx_stream(stream_args)` 得到 `rx_stream`。随后为每个通道 `new` 一块样本缓冲。注释解释了为何用裸数组而非 `std::vector`：`recv` 会对每个子数组 `reinterpret_cast<char*>`，这与 `std::vector` 内部布局不兼容。

下发流命令启动接收：

[host/examples/rx_samples_to_file.cpp:206-213](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L206-L213) —— 根据「是否指定样本总数」选择 `STREAM_MODE_START_CONTINUOUS`（连续接收）或 `STREAM_MODE_NUM_SAMPS_AND_DONE`（收够指定数量）；`stream_now` 对多通道置 `false` 以便对齐；`time_spec` 设为「现在 + 50 ms」让设备在确定时刻开始。`rx_stream->issue_stream_cmd(stream_cmd)` 下发命令。`stream_cmd_t` 定义在 [host/include/uhd/types/stream_cmd.hpp:41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/stream_cmd.hpp#L41)。

接收主循环（整个示例最关键的几行）：

[host/examples/rx_samples_to_file.cpp:226-232](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L226-L232) —— `while` 的三个退出条件：① 收到 `stop_signal_called`（Ctrl-C）；② 已收够请求样本数（或请求的是连续模式）；③ 超过 `time_requested` 时长。循环体内 `rx_stream->recv(buffs, samps_per_buff, md, 3.0, enable_size_map)` 阻塞接收一批样本，超时上限 3 秒，结果连同状态写入 `md`（`rx_metadata_t`，前置声明在 [stream.hpp:23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L23)）。

接收后立即根据 `md.error_code` 分流处理：

[host/examples/rx_samples_to_file.cpp:234-263](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L234-L263) —— `TIMEOUT` 直接 `break` 退出循环；`OVERFLOW` 打印一次提示后 `continue`（丢弃这批，不写盘）；其余错误按 `--continue` 决定继续还是抛异常。这三种错误码都来自 `uhd::rx_metadata_t`，进阶篇 u2-l6 会专门精读。

正常收到样本后写盘：

[host/examples/rx_samples_to_file.cpp:273-280](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L273-L280) —— 累加样本计数，对每个打开的通道把 `buffs[ch]` 强转为 `const char*` 写入对应的 `ofstream`。多通道时，文件名会自动加后缀（如 `lte_5mhz_ch0.dat`），命名逻辑见 [第 184-204 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L184-L204)。

循环结束后清理：

[host/examples/rx_samples_to_file.cpp:295-306](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L295-L306) —— 把流命令模式改成 `STREAM_MODE_STOP_CONTINUOUS` 并下发以停止设备；逐个关闭文件、`delete[]` 释放缓冲。

**阶段 4：多线程与清理（主函数末尾）**

[host/examples/rx_samples_to_file.cpp:734-768](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L734-L768) —— 默认单线程（`--multi-streamer` 未给时，循环里 `break` 只起一个线程，处理全部通道）；多线程模式则每通道一个线程，各自调 `recv_to_file`。线程函数里用 `type`/`otw` 组合选择模板实例（如 `recv_to_file<std::complex<float>>`）。

[host/examples/rx_samples_to_file.cpp:782-812](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L782-L812) —— 主线程的等待循环：周期 `sleep`，移除已结束的线程，按 `--progress` 打印平均吞吐（Msps），最终 join 所有残留线程。

**附：磁盘写速预估（仅 Linux）**

Linux 下示例还会跑一个 `dd` 测试估算磁盘能否跟上采样率，需要的最小磁盘写速为：

\[

\text{req\_disk\_rate} \;=\; \text{采样率} \;\times\; \text{通道数} \;\times\; \text{每样本字节数}

\]

这段逻辑见 [第 697-712 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L697-L712)，其中 `uhd::convert::get_bytes_per_item(otw)` 给出 `sc16` 每样本 4 字节（I、Q 各 2 字节）。若预估写速不够，会警告「可能溢出」。`convert` 子系统的细节在进阶篇 u4-l1。

#### 4.3.4 代码实践

**实践目标**：本讲的指定实践任务 —— **对照源码画出 `rx_samples_to_file` 的调用流程图，标出「设备构造」「采样率设置」「接收循环」三个阶段。**

**操作步骤**（纯源码阅读型，无硬件要求）：

1. 打开 [rx_samples_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp)，从 `UHD_SAFE_MAIN`（第 370 行）开始向下读。
2. 找到并记录下列三处「里程碑」对应的**行号**与**关键 API 调用**：
   - **设备构造**：搜索 `multi_usrp::make`（第 572 行附近），记录它前面打印的提示、后面紧跟的通道解析与 `set_clock_source` / `set_rx_subdev_spec`。
   - **采样率设置**：搜索 `set_rx_rate`（第 608 行附近），记录它前后的速率合法性校验与「实际采样率」回读。
   - **接收循环**：进入 `recv_to_file`，搜索 `while (not stop_signal_called`（第 226 行附近），记录循环体内 `recv`、`md.error_code` 三分支、`outfiles[ch].write` 的相对位置。
3. 用纸笔或画图工具，把这三段画成一张纵向流程图（可参考 4.3.2 的骨架）。

**需要观察的现象 / 思考点**：

- 「设备构造」阶段在调用 `make` **之后**、配置射频**之前**，插入了通道解析和子设备选择 —— 为什么顺序不能颠倒？（提示：子设备映射会影响通道编号）
- 「采样率设置」用 `ALL_CHANS` 一次性设置全部通道，而「频率设置」却用 `for (chan : channel_list)` 逐通道循环 —— 注意这种写法上的差异。
- 「接收循环」的三个退出条件如何用 `&&` / `||` 组合在一个 `while` 条件里。

**预期结果**：你能产出一张包含三个清晰阶段、标注关键行号与 API 的流程图，并能在图上指出 Ctrl-C（`stop_signal_called`）是如何让循环优雅退出的。

**待本地验证**：流程图本身无需运行；若想进一步验证，可在有 USRP 硬件时实际运行一条命令（如示例文档里的 LTE 捕获命令）并把控制台输出的阶段顺序与你的流程图对照。

#### 4.3.5 小练习与答案

**练习 1**：接收循环里有三个 `while` 退出条件（Ctrl-C、样本数达标、时长到达），但默认参数下 `total_num_samps=0` 且 `total_time=0`。此时程序靠什么退出？

**参考答案**：默认下 `num_requested_samples == 0` 表示「连续接收」，`time_requested == 0.0` 表示「不限时长」。两个条件都不触发退出，循环只能靠 Ctrl-C（`stop_signal_called` 被信号处理器置 `true`）来退出。这正是第 692-695 行在连续模式下才注册 `SIGINT` 处理器、并打印 `Press Ctrl + C to stop streaming...` 的原因。

**练习 2**：示例为什么强调「采样率设置后要回读 `get_rx_rate` 的实际值」？如果直接用用户请求的 `rate` 做后续计算会怎样？

**参考答案**：USRP 硬件只支持一组离散采样率，请求值会被四舍五入到最接近的支持值，因此「请求的 rate」与「实际的 rate」常常不相等。如果直接用请求值，后续对采集到的数据做 FFT、解调等处理时，时频轴标定就是错的（例如真实 7.68 Msps 却按请求的 8 Msps 处理，频谱会整体偏移）。所以示例和 `program_doc` 都反复提醒「always verify and use the actual sample rate」。

**练习 3**：单线程模式下，`recv_to_file` 用 `channel_list`（所有通道）创建**一个**流器；多线程模式（`--multi-streamer`）下，每个线程只放**一个**通道。这两种模式对写文件有什么不同影响？

**参考答案**：单线程模式下，一个流器把多个通道的样本交织在同一个 `recv` 里返回，`buffs[ch]` 是分开的数组，每通道写一个文件（文件名带 `_chN` 后缀）。多线程模式下，每个通道独立一个流器和线程，相互不阻塞，适合「单通道吞吐受 CPU 限制时」分摊负载；文件名前缀还会带线程标识（见 `recv_to_file_args` 宏里的 `"ch" + ... + "_" + file`）。

---

## 5. 综合实践

**综合任务：给 `rx_samples_to_file` 画一张「带行号标注」的完整生命周期时序图。**

把本讲三个模块的知识用起来，完成下面这张表（填写行号区间 + 一句话职责）：

| 阶段 | 起止行号 | 关键 API / 结构 | 一句话职责 |
|------|----------|----------------|-----------|
| 异常兜底入口 | `safe_main.hpp` L21-L34 | `UHD_SAFE_MAIN` 宏 | 把 `_main` 包进 try/catch |
| 命令行解析 | L455-L556 | `po::options_description`、`po::store`、`po::notify` | 声明并解析全部选项 |
| 设备构造 | L569-L600 | `multi_usrp::make`、`set_rx_subdev_spec` | 打开设备、选子设备 |
| 采样率设置 | L602-L612 | `set_rx_rate(ALL_CHANS)`、`get_rx_rate` | 设定并回读实际采样率 |
| 频率/增益/带宽/天线 | L614-L656 | `tune_request_t`、`set_rx_freq` 等 | 配置射频链 |
| LO 锁定等待 | L658-L690 | `check_locked_sensor` | 轮询 `lo_locked` 等传感器 |
| 接收循环（在 `recv_to_file`） | L226-L292 | `rx_stream->recv`、`md.error_code` | 收样本、判错、写盘 |
| 多线程调度 | L734-L768 | `std::thread`、模板实例选择 | 按通道开线程 |
| 主线程等待与清理 | L782-L812 | `join`、带宽统计 | 等待退出、汇报吞吐 |

完成表格后，**追问自己三个问题**：

1. 如果把「设备构造」里的 `set_rx_subdev_spec` 移到「采样率设置」之后，可能出什么问题？（通道映射尚未生效时设速率，作用对象可能不对。）
2. 如果用户既不给 `--nsamps` 也不给 `--duration`，程序如何退出？（只能靠 Ctrl-C，见练习 4.3.5 第 1 题。）
3. `recv` 返回 `OVERFLOW` 时，示例是写盘还是丢弃？为什么？（丢弃并 `continue`，因为溢出意味着样本已丢失、这一批不完整。）

**预期产出**：一张填满行号的时序表 + 三段简短分析。这是你后续阅读 `tx_waveforms`、`sync_to_gps` 等任何 UHD 示例时的通用「阅读模板」。

## 6. 本讲小结

- `UHD_SAFE_MAIN` 是一个宏，它把真正的 `main` 包进 `try/catch`，把未捕获异常转成可读的 `Error: ...` 与 `-1` 退出码；你写的函数体其实是 `_main`。
- `boost::program_options` 用「声明选项表 → `parse_command_line` → `notify`」三步把命令行字符串解析进 C++ 变量；带 `po::value(...)` 的选项要跟值，不带的是布尔开关，用 `vm.count(...)` 判断。
- 示例主流程分四阶段：**构造设备**（`multi_usrp::make`）→ **配置射频**（采样率/频率/增益/带宽/天线 + LO 锁定）→ **接收写文件**（`get_rx_stream` → `issue_stream_cmd` → `recv` 循环 → `write`）→ **多线程清理**。
- 接收循环的三种 `md.error_code` 分支：`TIMEOUT` 退出、`OVERFLOW` 丢弃继续、其它错误按 `--continue` 决定。
- 始终要回读「实际采样率」等回读值，因为硬件会把请求值四舍五入到最接近的支持值。
- 本讲建立的「四阶段阅读模板」适用于所有 UHD 收发示例。

## 7. 下一步学习建议

至此入门篇（u1）全部结束，你已经能读懂一个完整的 UHD 程序骨架。接下来进入**进阶篇 u2「核心驱动与流式传输」**，按以下顺序深入：

- **u2-l1 设备发现与工厂模式**：回到 `device::register_device / find / make`，理解 `multi_usrp::make(args)` 内部第一步「设备发现」是如何工作的。
- **u2-l3 multi_usrp 高层 API**：本讲把 `multi_usrp` 当黑盒，u2-l3 会打开它，讲清楚 `set_rx_rate`、`set_rx_subdev_spec`、通道映射规则等。
- **u2-l5 流式 API** 与 **u2-l6 接收流与元数据**：精读 `stream_args_t`、`rx_streamer::recv`、`rx_metadata_t` 各错误码，把本讲「点到的」接收细节补全。

建议你保留本讲产出的流程图 —— 学到 u2-l6 时，可以用更深的细节回填到同一张图上，形成一张「个人版 UHD 接收全链路图」。
