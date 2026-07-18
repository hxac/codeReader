# STM32 单元测试与 bug 回归

## 1. 本讲目标

AERIS-10 雷达的 STM32 固件跑在真实芯片上，但绝大多数缺陷完全可以在普通 PC 上被发现、被复现、被「锁死」——前提是有一套能让嵌入式代码脱离硬件编译运行的基础设施。本讲就拆解这套基础设施。

学完本讲，你应当能够：

1. 说清楚 **shim（垫片）** 与 **mock（替身）** 的区别，并能指出 STM32 固件里哪些接口必须被桩替换才能在 PC 上编译。
2. 读懂 `tests/Makefile` 的「五类测试」分组（real / mock-only / standalone / platform / C++），解释为什么同一个 bug 有时要「重放真实调用序列」、有时只要「验证一段纯逻辑」。
3. 理解 **bug 回归测试**（`test_bug1..15`）和 **Gap-3 安全测试**（`test_gap3_*`）各自守护什么，并能挑出一个具体用例说明它防止的缺陷。
4. 独立运行 `make test`，并对结果里的数字做出正确解读（包括「代码与注释不一致」时该信谁）。

## 2. 前置知识

本讲默认你已经读过 [u7-l1 STM32 main 与外设初始化](u7-l1-stm32-main-and-peripherals.md)，知道 STM32 是全板「系统管理者」，main.cpp 里调用了大量 STM32 HAL 函数（`HAL_GPIO_WritePin`、`HAL_SPI_TransmitReceive`、`HAL_Delay`…）以及 ADI 公司的 no-OS 驱动（`adf4382_init`、`ad9523_setup`…）。如果这些名词你完全陌生，建议先回到 u7-l1。

下面三个概念是本讲的地基，先用人话讲一遍：

- **宿主机（host）vs 目标机（target）**：固件最终运行在 STM32F746（target）上，但我们想在写代码的 Mac/Linux（host）上就直接编译运行它来抓 bug。问题是 host 上既没有 STM32 的硬件外设，也没有 ADI 的全套驱动源码。
- **桩/替身（stub / mock）**：给那些「target 上才有、host 上没有」的函数提供一个假的实现。假实现不真去操作硬件，而是把「被调用这件事」以及参数记到一本「账本」里，供测试事后核查。
- **垫片头文件（shim header）**：固件源码里写着 `#include "stm32f7xx_hal.h"`、`#include "adf4382.h"`。这些头在 host 上不存在。shim 就是同名、但内容指向 mock 的「替换头」，靠编译器搜索路径（`-I`）的先后顺序把真头「截胡」掉。

一句话区分 shim 与 mock：**shim 骗的是编译器的 `#include`，mock 骗的是运行时的函数实现。** 两者配合，才能让 main.cpp 这类深度依赖硬件的代码在 PC 上跑起来。

## 3. 本讲源码地图

本讲所有源码都集中在 MCU 固件的测试目录下，路径前缀统一是 `9_Firmware/9_1_Microcontroller/tests/`。

| 文件 | 作用 |
| --- | --- |
| `Makefile` | 测试入口：分组、编译、运行、计数通过/失败，是 `make test` 的总调度。 |
| `stm32_hal_mock.h` / `stm32_hal_mock.c` | STM32 HAL 的替身：定义假的外设句柄、GPIO 端口，并把每次 HAL 调用记进 `spy_log`。 |
| `ad_driver_mock.h` / `ad_driver_mock.c` | ADI 驱动（ADF4382 / AD9523）的替身：可注入返回值，模拟「初始化失败」「PLL 已锁定」等场景。 |
| `shims/*.h` | 一组垫片头：把 `stm32f7xx_hal.h`、`adf4382.h`、`no_os_spi.h` 等重定向到上面的 mock。 |
| `test_bug*.c`（15 个） | bug 回归测试：每个文件对应一个曾经修过的具体缺陷。 |
| `test_gap3_*.c`（7 个） | Gap-3 安全测试：守护功放、电源、温度、看门狗等「出事就烧硬件」的安全路径。 |
| `test_agc_outer_loop.cpp` / `test_um982_gps.c` | 额外两类：AGC 外环（C++）与 GPS 驱动白盒测试。 |
| `.github/workflows/ci-tests.yml` | CI 配置：`mcu-tests` job 用一行 `make test` 在 ubuntu 上跑全部用例。 |

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块：**shim/mock 测试**、**bug 回归**、**安全测试**。

### 4.1 shim/mock 测试：在 PC 上跑嵌入式固件

#### 4.1.1 概念说明

STM32 固件不能直接在 PC 上编译，原因有两层：

1. **头文件层**：main.cpp 与驱动源码里 `#include "stm32f7xx_hal.h"`、`#include "adf4382.h"`、`#include "no_os_spi.h"`，这些头要么是 ST 的专用库、要么是 ADI no-OS 框架的一部分，host 上没有。
2. **实现层**：即便头能编过，`HAL_GPIO_WritePin` 真去写 STM32 的 GPIO 寄存器、`adf4382_init` 真去通过 SPI 配置一颗 10.5 GHz 的频综芯片——host 上根本没有这些硬件，调用必然崩。

shim 解决第 1 层，mock 解决第 2 层。这套手法的核心价值是：**被测代码本身一字不改**（或只做最小抽取），改的是它「看到的世界」。这样测的是真实固件逻辑，而不是「为了能测而重写一遍」的影子代码。

#### 4.1.2 核心流程

一次主机端测试的典型生命周期：

```text
make test
  └─ 对 ALL_TESTS 里每个测试：
       1. 用 -Ishims -I. -I../9_1_1_C_Cpp_Libraries 编译测试 .c
          （-Ishims 排第一 ⇒ 同名真头被 shim 截胡）
       2. 链接 mock 目标（stm32_hal_mock.o / ad_driver_mock.o）
          必要时再链接真实源码目标（adf4382a_manager.o / platform_noos_stm32.o）
       3. 运行可执行文件：测试调「真实逻辑」→ 逻辑内部调 HAL/驱动
          → 全部落到 mock → mock 把每次调用 push 进 spy_log
       4. 测试用 assert 核对 spy_log 的内容（次数、顺序、参数）
       5. 退出码 0=通过 / 非0=失败，Makefile 计数
```

关键设计有三处：垫片的「搜索路径截胡」、mock 的「记账式 spy」、以及 mock 对返回值的「可注入」能力。下面逐个看源码。

#### 4.1.3 源码精读

**(1) 垫片靠 `-I` 顺序截胡真头。** Makefile 把 `-Ishims` 放在所有 `-I` 的最前面，注释直白写出意图：

[9_Firmware/9_1_Microcontroller/tests/Makefile:18-23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L18-L23) —— 编译选项里 `INCLUDES := -Ishims -I. -I../9_1_1_C_Cpp_Libraries`，注释明说「Shim headers come FIRST so they override real headers」。

垫片本身极简，只是把包含重定向到 mock。例如 `adf4382.h` 整个文件只有：

[9_Firmware/9_1_Microcontroller/tests/shims/adf4382.h:1-5](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims/adf4382.h#L1-L5) —— `#include "ad_driver_mock.h"`，于是固件源码里所有 `#include "adf4382.h"` 在测试编译时拿到的都是 mock 类型。

`no_os_spi.h`、`stm32f7xx_hal.h` 同理，见 [shims/no_os_spi.h:1-5](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims/no_os_spi.h#L1-L5) 与 [shims/stm32f7xx_hal.h:1-5](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims/stm32f7xx_hal.h#L1-L5)。较特别的是 `stm32_spi.h`，它不但重定向，还提供了一个只含「句柄 + CS 引脚」的精简 `stm32_spi_extra` 结构体和一个 extern 的 `stm32_spi_ops`，让真实源码 `adf4382a_manager.c` 能编过：

[9_Firmware/9_1_Microcontroller/tests/shims/stm32_spi.h:17-25](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims/stm32_spi.h#L17-L25) —— 定义测试版 `stm32_spi_extra` 并 extern 声明 `stm32_spi_ops`。

**(2) spy 记账式 mock。** `stm32_hal_mock.c` 为每个 GPIO 端口建一个带 `id` 的实例（这样 `assert(r->port == GPIOF)` 才有区分度），并维护一个全局调用日志 `spy_log`：

[9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c:10-16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L10-L16) —— `gpio_a..gpio_g` 各自带唯一 `id`；`GPIOF` 等宏是指向它们的指针。

[9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c:29-30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L29-L30) —— `SpyRecord spy_log[SPY_MAX_RECORDS]; int spy_count;` 就是那本「账本」。

每次 HAL 调用都把一条记录 `spy_push` 进账本。以 `HAL_GPIO_WritePin` 为例：

[9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c:137-146](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L137-L146) —— 不写真寄存器，只把 `{type, port, pin, value}` 记进 `spy_log`。

测试侧靠三个助手函数查账：`spy_reset()` 清空（[stm32_hal_mock.c:66-76](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L66-L76)）、`spy_count_type()` 按类型计数（[stm32_hal_mock.c:84-91](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L84-L91)）、`spy_find_nth()` 找第 n 次某类调用的下标（[stm32_hal_mock.c:93-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L93-L103)）。能记的调用类型在枚举里一一列出：

[9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.h:116-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.h#L116-L144) —— `SpyCallType` 覆盖 GPIO 读写/翻转、Delay/GetTick、UART 收发、ADF4382/AD9523 各驱动调用、SPI、PWM 等，是整个 spy 体系的「分类目录」。

**(3) 驱动 mock 的返回值注入。** HAL mock 只记不返数据，但驱动 mock 还要能模拟「初始化失败」「PLL 已锁定」这类条件。做法是把返回值做成全局变量，测试随时改：

[9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c:9-11](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c#L9-L11) —— `mock_adf4382_init_retval` 等三个全局开关。

`adf4382_init` 依据这个开关决定返回成功还是失败，失败时把 `*device` 置 NULL（这正是 4.2 要测的 Bug #4 触发点）：

[9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c:39-54](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c#L39-L54) —— 非零 retval ⇒ `*device=NULL` 并返回错误；否则发一个堆上的 stub 设备。

`adf4382_spi_read` 则在读到寄存器 `0x58`（锁相检测位）时返回「已锁定」，让上层 `ADF4382A_CheckLockStatus` 在测试里默认拿到 lock OK：

[9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c:92-104](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.c#L92-L104) —— `reg_addr==0x58` ⇒ `*data = ADF4382_LOCKED_MSK`（掩码定义见 [ad_driver_mock.h:101](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/ad_driver_mock.h#L101)）。

**(4) `stm32_spi_ops` 的非空占位。** 真实源码 `adf4382a_manager.c` 引用了 `&stm32_spi_ops`，测试里 `adf4382_init` 被替身接管、永远不会真去调 `no_os_spi_init()`，但链接器仍需要一个非空的符号。mock 提供一个返回 0 的桩函数撑起这个结构体：

[9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c:399-408](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L399-L408) —— 注释点明「tests 里 adf4382_init 被替身接管，no_os_spi_init 不会被调用，这里只保证 `platform_ops != NULL`」。

这正是本模块第三条学习目标「能识别哪些 no-OS 接口需要被桩替换」的活样本：需要桩的是那些被真实源码**引用、但运行时不会被真正执行**的符号。

#### 4.1.4 代码实践

> **实践目标**：亲眼确认 shim 的「截胡」真的发生，并理解一次 HAL 调用是如何变成 spy 记录的。

**操作步骤（源码阅读型 + 可选运行）**：

1. 在 `tests/` 下确认 `shims/` 目录里没有真正的 ST 库，只有 9 个重定向小头。对照 [shims/no_os_delay.h:1-8](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims/no_os_delay.h#L1-L8)，说明 `no_os_udelay/no_os_mdelay` 的声明其实来自 `stm32_hal_mock.h`。
2. 在 `Makefile` 里找到 `INCLUDES`，把 `-Ishims` 临时挪到最后（例如改成 `-I. -I../9_1_1_C_Cpp_Libraries -Ishims`），然后运行 `make clean && make build`，观察编译错误（预期：找不到真头或类型冲突）。**这是破坏性观察，做完务必改回。**
3. 运行 `make test_bug2`（单独编一个 mock-only 用例），观察输出里 `SPY_AD9523_SETUP records: 1` 之类的打印，确认 spy 在工作。

**需要观察的现象**：步骤 2 会因为真头缺失或类型不一致而报错，反证 `-Ishims` 必须在最前；步骤 3 的输出里能看到「Reset release at spy index …, setup at …」这样的日志，说明 mock 不只是「能编过」，而是把调用顺序都记了下来。

**预期结果**：步骤 3 通过、打印 `ALL TESTS PASSED`。步骤 2 的确切报错信息取决于你机器上是否有 ST 头缓存，若不确定具体文本可记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GPIOF` 能在 `assert(r->port == GPIOF)` 里被当成「身份标识」？如果 mock 里所有端口共用一个实例会怎样？

**参考答案**：因为 mock 给 `gpio_a..gpio_g` 各自分配了不同的 `id` 字段，且 `GPIOF` 是指向 `gpio_f` 的指针，所以指针比较能区分端口。若共用一个实例，所有端口的 `WritePin` 在 spy 里都会指向同一地址，测试就无法判断「这次写到底打到了 GPIOF 还是 GPIOG」，顺序与归属断言全部失效。

**练习 2**：`ad_driver_mock.c` 里的 `adf4382_stub_devs[4]` 为什么是 4 个槽位？

**参考答案**：双 LO 管理器要初始化 TX 与 RX 两片 ADF4382（见 u7-l2），加上去程/重试可能多次调用，mock 用 4 槽位的环形分配（`adf4382_stub_idx % 4`）保证每次成功 init 返回一个独立的、清零过的 stub 设备，避免两次 init 拿到同一块内存互相污染。

---

### 4.2 bug 回归：把每个修过的缺陷锁死

#### 4.2.1 概念说明

**回归测试（regression test）** 的目的是：一个曾经出现、后来被修好的 bug，**永远不要再悄悄回来**。嵌入式项目里 bug 复发的高危场景是「重构 main.cpp 时不小心把修复又改回去了」——因为 main.cpp 体量巨大、初始化序列交错，肉眼很难看出哪一行是「关键防线」。

AERIS-10 的做法是给编号 Bug #1..#15 各写一个独立测试 `test_bugN_*.c`。这些测试不是测「功能对不对」，而是测「那个具体的坑还在不在」。因为 main.cpp 本身无法在 PC 上整体编译（它含 `main()`、中断、CubeMX 生成代码），所以测试普遍采用**抽取（extract）手法**：把出问题的几行逻辑原样复制到测试里的 `static` 函数，对它跑断言。注释里通常会写明「Extracted from main.cpp — FIXED version」。

#### 4.2.2 核心流程

一个典型 bug 回归测试的骨架：

```text
1. 在文件头注释里写清：Bug #N 是什么、修前什么样、修后什么样、本测试怎么验证。
2. 把 main.cpp 里出问题的逻辑段抽取成一个 static 函数（命名 *_extracted）。
3. 在 main() 里：
     a. spy_reset() 清账本；
     b. （可选）设 mock 返回值模拟成功/失败两条路径；
     c. 调抽取出来的逻辑；
     d. 用 assert 核对：是否调用了该调的、是否没调用不该调、顺序对不对、返回值对不对。
4. 全部 assert 通过 ⇒ return 0（Makefile 计为 PASS）；任一 assert 失败 ⇒ 进程非 0 退出（FAIL）。
```

Makefile 按测试**需要的链接依赖**把它们分成几组，而不是按 bug 编号：

[9_Firmware/9_1_Microcontroller/tests/Makefile:49-55](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L49-L55) —— `TESTS_WITH_REAL`：需要链接**真实源码** `adf4382a_manager.o` + mock 的 7 个测试（bug1/3/4/5/9/10/15）。

[9_Firmware/9_1_Microcontroller/tests/Makefile:58-63](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L58-L63) —— `TESTS_MOCK_ONLY`：只要 mock、不要真实源码的 6 个（含 bug2/6/7/8/14）。

[9_Firmware/9_1_Microcontroller/tests/Makefile:66-73](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L66-L73) —— `TESTS_STANDALONE`：**纯逻辑**、连 mock 都不链的 8 个（bug12/13 + 6 个 gap3），编译命令里只有测试 .c 本身。

这种分组揭示了一条重要经验：**测试该多重，取决于被测逻辑依赖了什么**。要验证「真的调到 ADI 驱动并按对顺序写 GPIO」就得链真实源码 + mock（如 Bug #4）；要验证「一个循环条件的数学对不对」什么依赖都不需要（如 Bug #12）。

#### 4.2.3 源码精读

**案例 A：Bug #4 —— init 失败时不应继续做相位装载（重放真实序列型）。**

缺陷描述：旧版 main.cpp 先调 `ADF4382A_Manager_Init`，**不检查返回值**就直接调 `SetPhaseShift` / `StrobePhaseShift`；一旦 init 失败、manager 没初始化好，后续相位操作就会作用在一个未就绪的对象上。修复后改成「先检查、失败即进 `Error_Handler()`」。

测试把修复后的序列抽出来，并设计两条路径：

[9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c:31-54](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c#L31-L54) —— 注释 `[Bug #4 FIXED] Error check happens FIRST`；`if (ret != ADF4382A_MANAGER_OK) { Error_Handler(); return 1; }` 在相位调用之前。

**成功路径**断言相位确实被调、init 被调了正好 2 次（TX+RX）：

[9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c:62-83](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c#L62-L83) —— 设 `mock_adf4382_init_retval = 0`，断言 `phase_shift_called == 1`、`SPY_ADF4382_INIT` 次数为 2。

**失败路径**（回归的核心）把 init 返回值改成 -1，断言 `Error_Handler` 被调、相位**没有**被调：

[9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c:85-100](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug4_phase_shift_before_check.c#L85-L100) —— `mock_adf4382_init_retval = -1` ⇒ `assert(error_handler_called == 1)` 且 `assert(phase_shift_called == 0)`。这条断言就是「Bug #4 不许回来」的锁。

注意这里同时演示了 4.1.3 里「返回值注入」的威力：不靠真实硬件、只改一个全局变量，就能逼出 init 失败这条罕见但致命的分支。

**案例 B：Bug #12 —— PA 校准循环条件写反（纯逻辑型）。**

缺陷描述：PA 的 Idq 闭环校准循环（详见 u7-l3）条件把不等号写反了——旧版 `abs(Idq-1.680) < 0.2` 意思是「**靠近**目标才继续转」，结果反而「靠近就停、远离才继续」，校准永远到不了目标。修复改成 `> 0.2`（「**远离**目标才继续，靠近就退出」）。

这种 bug 纯粹是逻辑错误，不需要任何硬件替身，测试只验证那个布尔表达式：

[9_Firmware/9_1_Microcontroller/tests/test_bug12_pa_cal_loop_inverted.c:22-26](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug12_pa_cal_loop_inverted.c#L22-L26) —— `should_continue_loop` 返回 `(DAC_val > 38 && fabs(Idq - 1.680) > 0.2)`，注释指明「matches the FIXED condition」。

随后用 7 组边界值（远、近、恰等于、DAC 到下限、刚好在容差外/内 0.201/0.199）夹击这个表达式，并跑一段完整收敛模拟确认能在有限步内停：

[9_Firmware/9_1_Microcontroller/tests/test_bug12_pa_cal_loop_inverted.c:32-84](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug12_pa_cal_loop_inverted.c#L32-L84) —— 注意 Test 5/6 用 0.201 与 0.199 两个值卡住容差边界，这正是回归测试该有的「贴着边走」的用例设计。

**案例 C：Bug #2 —— AD9523 重复 setup（顺序型，靠 spy 查次数与次序）。**

缺陷描述：旧版 `configure_ad9523()` 在释放复位**之前**就调过一次 `ad9523_setup()`，复位后又调一次——第一次 setup 作用在处于复位态的芯片上是无效甚至有害的。修复后只保留复位之后那一次。

测试重放修复后的序列，然后用 spy 查两件事：`ad9523_setup` 调用**次数**正好是 1，且「释放复位的 GPIO 写」在 setup **之前**：

[9_Firmware/9_1_Microcontroller/tests/test_bug2_ad9523_double_setup.c:75-100](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug2_ad9523_double_setup.c#L75-L100) —— `spy_count_type(SPY_AD9523_SETUP)` 断言为 1；再用 `spy_find_nth` 拿到 setup 的下标，向前搜索确认「GPIOF/PIN_6/SET」的复位释放写发生在它之前（`reset_gpio_idx < setup_idx`）。

这三个案例合起来展示了 bug 回归测试的三种武器：**注入返回值逼出失败分支**（Bug #4）、**纯逻辑边界夹击**（Bug #12）、**spy 计数 + 顺序断言**（Bug #2）。

#### 4.2.4 代码实践

> **实践目标**：任选一个 `test_bug*`，说清它防止的具体缺陷，并能单独运行它。

**操作步骤**：

1. 在 `tests/` 下任选一个文件，例如 `test_bug4_phase_shift_before_check.c`，读它的**文件头注释**（前 11 行）——它通常会一句话写清「修前/修后」。
2. 运行 `make test_bug4`（这是 Makefile 的便捷目标，见 [Makefile:228-229](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L228-L229)），它等价于「编译并立即运行该用例」。
3. 想象把被抽取的逻辑改坏（例如把 Bug #4 的 `if (ret != OK)` 删掉），预测哪个 `assert` 会先炸、进程退出码会变成几。
4. （可选）真把那行注释掉再 `make test_bug4`，验证你的预测。**这是破坏性操作，做完务必 `git checkout` 还原。**

**需要观察的现象**：正常情况下每个用例末尾打印 `=== Bug #N (FIXED): ALL TESTS PASSED ===` 并以退出码 0 返回；故意改坏后，`assert` 触发 `Aborted`、退出码非 0。

**预期结果**：`make test_bug4` 通过。若你跳过步骤 4，把「改坏后第几个 assert 失败」记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：Bug #4 的测试为什么要同时跑「成功」和「失败」两条路径，只跑成功路径够不够？

**参考答案**：不够。Bug #4 的本质是「init 失败这条分支没有正确拦截」，成功路径根本不会经过 `if (ret != OK)`，单测成功路径等于没测到 bug。必须用 `mock_adf4382_init_retval = -1` 注入失败，并断言此时相位函数没被调，才算把这条防线锁住。

**练习 2**：Bug #12 为什么放进 `TESTS_STANDALONE`、连 mock 都不链？

**参考答案**：因为它的缺陷完全是一个布尔表达式的数学错误，被测函数 `should_continue_loop` 不依赖任何 HAL 或驱动——不需要 spy、不需要替身、甚至不需要 `-Ishims`。把它放进 standalone 组是为了用最少的编译依赖最快地跑完，这也是「按依赖分组」原则的体现。

**练习 3**：Bug #2 的测试用 `spy_find_nth(SPY_AD9523_SETUP, 0)` 拿到下标，第二个参数 `0` 是什么意思？

**参考答案**：`0` 表示「找第 0 次（即第一次）`SPY_AD9523_SETUP` 调用在 `spy_log` 中的下标」，函数是 0 基的（见 [stm32_hal_mock.c:93-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/stm32_hal_mock.c#L93-L103)）。因为修复后 setup 只会被调一次，找第一次就够了。

---

### 4.3 安全测试：Gap-3 守护功放与电源

#### 4.3.1 概念说明

AERIS-10 Extended 版用 10 W GaN 功放（PA），工作在 >75 °C 仍偏置着是大忌——轻则烧管、重则起火。这类「出事就烧硬件」的路径，单列一组 **Gap-3 安全测试**（`test_gap3_*`）。

「Gap-3」可理解为「第三轮安全审查中发现并补上的缺口」。它和 bug 回归测试的区别在于**关注点**：bug 回归盯的是「某个已知的实现错误」，Gap-3 盯的是「安全状态机是否在每种该停机的情况下都真的停机」。一个典型的安全判据形如「当错误码属于 {过流、过温、看门狗超时、电源故障…} 时，必须立即 `Emergency_Stop()`」——少匹配一个枚举值就可能让某类故障「漏网」继续带电运行。

#### 4.3.2 核心流程

Gap-3 测试有两种写法，对应两种被测对象：

```text
A. 顺序/动作型（mock-only）：被测对象是「一连串 GPIO 写」（如 Emergency_Stop 关断电源轨）。
   ⇒ 用 spy 记录序列，逐条核对端口/引脚/电平与顺序。

B. 谓词型（standalone）：被测对象是「一个布尔判据」（如 error 是否触发紧急停机）。
   ⇒ 把判据抽成函数，遍历全部枚举值，正例必须返回真、负例必须返回假。
```

无论哪种，关键都是**穷举边界**：把 SystemError 枚举的每一项都过一遍，确保「该停的全停、不该停的不误停」。

#### 4.3.3 源码精读

**案例 A：过温/看门狗必须触发紧急停机（谓词型）。**

缺陷描述：旧版 `handleSystemError()` 的关键错误闸门写成区间判断 `if (error >= ERROR_RF_PA_OVERCURRENT && error <= ERROR_POWER_SUPPLY)`。问题是 `ERROR_TEMPERATURE_HIGH`（过温）和 `ERROR_WATCHDOG_TIMEOUT` 的枚举值落在这个区间**之外**，于是过温故障漏到了「记日志并继续」的分支，PA 在 >75 °C 仍被偏置着。修复在区间之外又显式 OR 上这两项。

测试镜像了 SystemError 枚举（注释要求与 main.cpp 保持 lockstep），并抽出修复后的判据：

[9_Firmware/9_1_Microcontroller/tests/test_gap3_overtemp_emergency_stop.c:27-53](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_gap3_overtemp_emergency_stop.c#L27-L53) —— `SystemError_t` 枚举逐项镜像；`triggers_emergency_stop` 返回 `(区间) || e==ERROR_TEMPERATURE_HIGH || e==ERROR_WATCHDOG_TIMEOUT`。

随后用 14 个断言穷举：5 个区间内错误 + 过温 + 看门狗（共 7 个正例）必须触发；NONE、时钟、PLL 失锁、通信、内存等 7 个负例必须不触发：

[9_Firmware/9_1_Microcontroller/tests/test_gap3_overtemp_emergency_stop.c:59-115](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_gap3_overtemp_emergency_stop.c#L59-L115) —— Test 6/7 标注 `(regression)`，正是本 bug 的回归点；Test 10 强调 `ERROR_ADF4382_TX_UNLOCK` 是「可恢复」故**不应**误停机。

这条测试揭示了一个安全设计原则：**「可恢复故障」与「致命故障」必须分桶**。PLL 失锁、通信超时这类可能自愈的错误，若也触发硬停机，会让系统动辄瘫痪；而过流、过温、看门狗超时这类不可恢复的硬件级危险，则绝不能漏。

**案例 B：Emergency_Stop 必须真切断 PA 电源轨（顺序型）。**

缺陷描述：旧版 `Emergency_Stop()` 只用 CLR 引脚清掉 DAC 栅压，但 PA 的 VDD 电源轨（5V0_PA1/2/3、5V5_PA、RFPA_VDD）仍带电，GaN 管可能自偏置或自激。修复增加了对 TX 混频器和全部 PA 电源轨的拉低。

因为真实的 `Emergency_Stop()` 内含死循环（安全锁死，见 u7-l1「紧急停机死循环」），没法直接调用，测试改为**重放它应做的 GPIO 序列**再用 spy 核对：

[9_Firmware/9_1_Microcontroller/tests/test_gap3_emergency_stop_rails.c:32-44](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_gap3_emergency_stop_rails.c#L32-L44) —— 模拟「先关 TX 混频器，再关 3 路 5V0、5V5、RFPA_VDD」的 6 次写。

测试断言 spy 里正好 6 条记录，且端口/引脚/电平、**顺序**与设计一致，并交叉校验引脚定义宏与硬件映射吻合：

[9_Firmware/9_1_Microcontroller/tests/test_gap3_emergency_stop_rails.c:67-96](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_gap3_emergency_stop_rails.c#L67-L96) —— `assert(spy_count == 6)`；`spy_log[0]` 必须是 TX 混频器先关（确保「先断激励、再断电源」的安全次序）；Test 3 校验 `EN_P_5V0_PA1_Pin == GPIO_PIN_0` 等引脚映射。

注意一个工程现实：这类「重放序列」的测试**强依赖测试函数与真实函数保持同步**——一旦真实 `Emergency_Stop()` 多关了一路或少关了一路，测试函数若不同步改动就测不出来。这是抽取式测试的固有局限，缓解办法是配合 u11-l3 的跨层契约测试做交叉验证。

其余 Gap-3 用例覆盖：IWDG 看门狗配置（`test_gap3_iwdg_config`）、温度上限（`test_gap3_temperature_max`）、Idq 周期性重读（`test_gap3_idq_periodic_reread`）、紧急状态进入次序（`test_gap3_emergency_state_ordering`）、冷启动健康看门狗（`test_gap3_health_watchdog_cold_start`）。它们都属于上述两种范式之一。

#### 4.3.4 代码实践

> **实践目标**：单独运行一个 Gap-3 安全测试，并理解「区间判断漏枚举」为何危险。

**操作步骤**：

1. 运行 `make test_gap3_overtemp`（便捷目标见 [Makefile:279-280](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L279-L280)）。
2. 阅读输出里 Test 6（`ERROR_TEMPERATURE_HIGH`）和 Test 7（`ERROR_WATCHDOG_TIMEOUT`）这两行——它们就是本 bug 的回归锚点。
3. 思考题：如果把 `triggers_emergency_stop` 改回「只用区间判断」（删掉两个 `||`），Test 6/7 会怎样？Test 1–5 呢？

**需要观察的现象**：14 个断言全部 PASS，末尾打印 `Safety fix: ALL TESTS PASSED`。

**预期结果**：通过。步骤 3 的预测：Test 1–5（区间内）仍 PASS，但 Test 6/7 会 FAIL（`triggers_emergency_stop` 对过温/看门狗返回假）——这正说明区间判断会漏掉这两类故障。准确行为以本地复现为准，若未实跑记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `triggers_emergency_stop` 不干脆把所有错误都判为「触发紧急停机」，这样不是最安全吗？

**参考答案**：那样会把「可恢复故障」（PLL 暂时失锁、某路传感器通信超时、内存分配失败）也升级成硬停机，导致系统频繁瘫痪、可用性归零。安全设计要兼顾「该停的必停」与「不该停的不误停」，所以必须分桶：只有不可恢复的硬件级危险（过流、过温、看门狗、电源）才进紧急停机。

**练习 2**：`test_gap3_emergency_stop_rails.c` 为什么不直接调用真实的 `Emergency_Stop()`，而是重放一段 GPIO 序列？

**参考答案**：因为真实的 `Emergency_Stop()` 末尾是死循环（故意锁死在安全状态以禁止自动恢复，见 u7-l1），直接调用会让测试进程永远挂住、Makefile 计数为 FAIL。重放序列既验证了「该关的轨都被关、顺序正确」，又避开了死循环——代价是测试与真实函数需人工保持同步。

---

## 5. 综合实践

把三个模块串起来，完成一次「从基础设施到具体缺陷」的完整端到端验证。

**任务**：在 `9_Firmware/9_1_Microcontroller/tests/` 下完成下列步骤并记录结果。

1. **运行全套**：执行 `make test`，把末尾的汇总行 `Results: X passed, Y failed (of Z total)` 抄下来。
2. **核对计数（关键，练「读代码不读注释」）**：对照 Makefile 里 `ALL_TESTS` 的六个分组，**手数**一遍：
   - bug 回归：bug1..bug15 共 **15** 个；
   - Gap-3 安全：共 **7** 个（`emergency_stop_rails`、`iwdg_config`、`temperature_max`、`idq_periodic_reread`、`emergency_state_ordering`、`overtemp_emergency_stop`、`health_watchdog_cold_start`）；
   - 另有 `test_agc_outer_loop`（C++）与 `test_um982_gps`（GPS）各 1 个，共 2 个；
   - 合计 **24** 个（即 `$(words $(ALL_TESTS))` 应打印 24）。
3. **发现不一致**：打开 [.github/workflows/ci-tests.yml:54-56](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L54-L56)，看到 CI 注释写着「MCU Firmware Unit Tests (20 tests) // Bug regression (15) + Gap-3 safety tests (5)」。这个 **20/5** 与你手数的 **24/7** 对不上——注释已经滞后于代码。请用一句话解释该信哪个、为什么。
4. **挑一个 bug 深读**：从 15 个 `test_bug*` 里任选一个，写一份三行说明：①它防止的缺陷；②它用了 4.2.3 里的哪种武器（注入失败分支 / 纯逻辑边界 / spy 计数与顺序）；③如果不写这个测试，bug 最可能在什么场景下复发。
5. **CI 串联**：确认 [.github/workflows/ci-tests.yml:67-69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L67-L69) 里 `mcu-tests` job 干的事就是 `make test`（working-directory 指向 tests 目录）。也就是说，你本地的 `make test` 与 CI 跑的是**同一条命令**——任何本地能复现的失败，CI 也能拦住。

**交付物**：一份包含步骤 1 的实测汇总行、步骤 2 的手数清单、步骤 3 的一句话裁决、步骤 4 的三行说明的简短笔记。

**关于可信度**：步骤 1 的精确 X/Y 取决于你机器上的编译器与依赖是否齐全，若未实跑请如实标注「待本地验证」；但步骤 2 的「15 + 7 + 2 = 24」是从源码静态数出来的，是确定的。

## 6. 本讲小结

- **shim 骗编译、mock 骗运行**：垫片头靠 `-Ishims` 排在最前截胡真头（`adf4382.h`/`stm32f7xx_hal.h`/`no_os_spi.h` 等），HAL 与驱动 mock 把每次调用记进 `spy_log`，两者配合让 main.cpp 的逻辑在 PC 上可编译可运行。
- **mock 的两大能力**：记账（`spy_count_type`/`spy_find_nth` 查次数与顺序）与返回值注入（`mock_adf4382_init_retval` 等逼出失败分支），是覆盖「罕见但致命路径」的关键。
- **被测代码不改、只改它看到的世界**：bug 测试普遍用「抽取」手法把 main.cpp 的出问题逻辑原样复制进 `static` 函数，测的是真实固件逻辑而非影子代码。
- **Makefile 按链接依赖分组**：real（链真实源码 + mock）/ mock-only / standalone（纯逻辑）/ platform / C++ —— 测试多重取决于被测逻辑依赖了什么。
- **bug 回归 = 把每个修过的缺陷锁死**，三种武器：注入失败分支（Bug #4）、纯逻辑边界夹击（Bug #12）、spy 计数 + 顺序断言（Bug #2）。
- **Gap-3 安全测试盯的是「该停必停、不该停不误停」**：过温/看门狗漏触发（谓词型）、Emergency_Stop 没真断电源轨（顺序型）都是会烧硬件的缺陷，必须穷举枚举值与 GPIO 顺序。
- **读代码不读注释**：CI 注释写「20 tests / 5 gap3」，Makefile 实际是「24 tests / 7 gap3」——计数以 `$(words $(ALL_TESTS))` 为准。

## 7. 下一步学习建议

1. **横向对比 FPGA 回归（u11-l1）**：同样是在 host 上跑 target 代码，FPGA 用 iverilog + 真实数据 exact-match，MCU 用 shim/mock + spy。体会「嵌入式软件测试」与「硬件描述语言测试」在手法的同与不同。
2. **进入跨层契约测试（u11-l3）**：本讲的抽取式测试有一个固有局限——测试函数与真实函数靠人工保持同步。u11-l3 的 `test_cross_layer_contract.py` 用三层独立真值推导（静态解析 / iverilog cosim / C 执行）来补这个洞，是本讲的自然进阶。
3. **回到 u7 系列对照**：把 Bug #4（频综 init）、Bug #12（Idq 校准）、Gap-3 过温停机分别对照 u7-l2（时钟频综）、u7-l3（ADAR1000/Idq）、u7-l1（电源时序与安全权威），你会看到「测试守护的正是那些讲义里强调的安全关键路径」。
4. **动手扩展**：仿照 `test_bug12`（纯逻辑）的写法，为 main.cpp 里任意一个有边界条件的算法（如某个阈值判断、某个有限状态机的迁移）补一个 standalone 回归测试，走通「写测试 → make test → 看 PASS」的完整闭环。
