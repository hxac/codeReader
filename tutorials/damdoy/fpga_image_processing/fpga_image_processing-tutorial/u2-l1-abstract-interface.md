# 抽象基类与命令枚举：贯穿全项目的契约

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `software/image_processing.hpp` 里的纯虚基类 `Image_processing` 为什么能让**同一份 `main.cpp`** 同时驱动仿真后端和 iCE40 硬件后端。
2. 记住每一个纯虚函数分别对应硬件侧哪条 `COMMAND_*` 命令、参数含义是什么、是否需要在调用后 `wait_end_busy`。
3. 看懂 `Commands` 枚举的数值与硬件侧 `hdl/image_processing.v` 里的 `parameter COMMAND_*` 是如何一一对应的——这是软件世界与硬件世界之间最重要的一份「契约」。
4. 理解 `send_clear` 为什么能被实现为 `send_threshold(0, value, true)` 的复用，体会「接口设计」如何把硬件复杂度藏起来。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（来自第 1 单元）：

- **项目是「一个核心 HDL 模块 + 两套可替换后端」**：核心模块 `image_processing.v` 只认「命令 + 数据」；仿真后端用 Verilator 把它编成 C++ 模型 `Vimage_processing`，硬件后端经 SPI 与真实芯片通信。
- **`main.cpp` 是唯一的主机入口**，它按 `send_params → send_image → switch_buffers → 发运算 → wait_end_busy → switch_buffers → read_image` 这样的「三明治」套路调用后端。
- **C++ 多态**：基类指针调用虚函数时，实际执行的是指针所指向的派生类的实现。本讲正是用这个机制实现「一份代码，两个后端」。
- **命令（command）**：主机发给硬件的一条指令，由「1 字节操作码 + 变长参数」组成。本讲只关心操作码这层映射，字节级打包细节留给下一讲 `u2-l2`。

如果你对「双后端」「`-DSIMULATION` 宏切换后端」「三明治调用套路」这些词还陌生，建议先回看 `u1-l1`、`u1-l3`、`u1-l5`。

## 3. 本讲源码地图

本讲只聚焦主机侧的「契约层」，涉及的关键文件如下：

| 文件 | 角色 | 本讲解读的部分 |
| --- | --- | --- |
| `software/image_processing.hpp` | **契约本身**：定义 `Commands` 枚举与纯虚基类 `Image_processing` | 全文（仅 42 行） |
| `software/main.cpp` | **契约的使用者**：用基类指针多态调用各后端 | `#ifdef` 选后端、`test_add_threshold` |
| `simulation/image_processing_simulation.cpp` | 仿真后端：把每个虚函数翻译成 FIFO 命令 | 用作「函数→COMMAND 映射」的佐证 |
| `ice40/software/image_processing_ice40.cpp` | 硬件后端：把每个虚函数翻译成 SPI 事务 | 同上，确认两套后端映射一致 |
| `hdl/image_processing.v` | 硬件侧命令的实际执行者 | `parameter COMMAND_*` 与阈值运算逻辑 |

核心结论先抛出来：**`image_processing.hpp` 是全项目最薄、却最关键的一份文件。它定义的 `Commands` 枚举和 `Image_processing` 纯虚基类，是主机软件、仿真后端、硬件后端、以及 HDL 模块四方共同遵守的契约。** 后续每一讲的细节，本质上都是这份契约的某种实现。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：`Commands` 枚举、`Image_processing` 纯虚接口、受保护成员 `image_width/image_height`、多态后端选择（`#ifdef`）、以及 `send_clear` 复用案例。

### 4.1 Commands 枚举：操作码的全项目契约

#### 4.1.1 概念说明

硬件只能听懂数字。当主机想对 FPGA 说「请把整幅图每个像素加 32」时，它不能发一句自然语言，而必须发一个**操作码（opcode）**——一个约定好的数字，比如「4 表示加法」。这个「数字 ↔ 含义」的对照表，就是 `Commands` 枚举。

`Commands` 的特殊之处在于它**横跨软件和硬件两个世界**：

- 在 C++ 侧（`image_processing.hpp`），它是一个 `enum`，主机和两套后端都用它来标识命令。
- 在 Verilog 侧（`image_processing.v`），它是一组 `parameter` 常量，状态机用它来判断「这条命令要我干什么」。

只要这两侧的数值保持一致，主机发出的字节流就能被硬件正确解读。这份「数值一致」就是全项目最核心的契约。

#### 4.1.2 核心流程

`Commands` 枚举的取值是**连续自增**的，从 0 开始：

```
COMMAND_PARAM=0, COMMAND_SEND_IMG=1, COMMAND_READ_IMG=2,
COMMAND_GET_STATUS=3, COMMAND_APPLY_ADD=4, COMMAND_APPLY_THRESHOLD=5,
COMMAND_SWITCH_BUFFERS=6, COMMAND_BINARY_ADD=7, COMMAND_APPLY_INVERT=8,
COMMAND_CONVOLUTION=9, COMMAND_BINARY_SUB=10, COMMAND_BINARY_MULT=11,
COMMAND_APPLY_MULT=12, COMMAND_NONE=255
```

按职责可以分成四组：

| 组 | 命令 | 数值 | 职责 |
| --- | --- | --- | --- |
| 配置与传输 | `COMMAND_PARAM`、`COMMAND_SEND_IMG`、`COMMAND_READ_IMG` | 0、1、2 | 设置图像尺寸、载入图像、回读图像 |
| 状态 | `COMMAND_GET_STATUS` | 3 | 查询硬件是否忙碌 |
| 逐像素运算（unary） | `COMMAND_APPLY_ADD`、`COMMAND_APPLY_THRESHOLD`、`COMMAND_APPLY_INVERT`、`COMMAND_APPLY_MULT` | 4、5、8、12 | 对单个缓冲做逐像素处理 |
| 双图运算（binary） | `COMMAND_BINARY_ADD`、`COMMAND_BINARY_SUB`、`COMMAND_BINARY_MULT` | 7、10、11 | 同时读两个缓冲做运算 |
| 卷积 | `COMMAND_CONVOLUTION` | 9 | 3×3 邻域卷积 |
| 缓冲管理 | `COMMAND_SWITCH_BUFFERS` | 6 | 交换 input/storage 两个缓冲 |
| 哨兵 | `COMMAND_NONE` | 255 | 占位/无效标记，不对应真实硬件命令 |

注意一点：`COMMAND_NONE=255` 是一个**哨兵值（sentinel）**，它故意被设成 255（远离正常命令的 0~12），用来表示「没有命令」。这个值**只存在于 C++ 侧**，在 HDL 的 `parameter` 列表里找不到对应——因为它从不真正发往硬件。

#### 4.1.3 源码精读

C++ 侧的枚举定义在文件开头：

[software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6) —— 定义 `Commands` 枚举，从 `COMMAND_PARAM=0` 开始，末尾用 `COMMAND_NONE=255` 作哨兵。

硬件侧用 `parameter` 常量定义了**完全相同的数值序列**，靠 `前一个+1` 的链式表达式保证顺序：

[hdl/image_processing.v:63-68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L63-L68) —— Verilog 侧的命令常量，`COMMAND_PARAM=0`，其余逐个 `+1`，顺序与 C++ 枚举逐字对应。

> 为什么硬件侧用 `COMMAND_SEND_IMG = COMMAND_PARAM+1` 这种链式写法，而不是直接写 `= 1`？因为这样**插入或删除一个命令时，后面的数值会自动顺延**，减少手动维护数值出错的概率。而 C++ 的 `enum` 本来就自动递增，所以两侧天然能保持一致。

#### 4.1.4 代码实践

**实践目标**：亲手验证软件侧枚举与硬件侧 parameter 的数值一一对应。

**操作步骤**：

1. 打开 `software/image_processing.hpp` 第 4–6 行，数出每个命令的序号（第一个是 0，之后依次 +1）。
2. 打开 `hdl/image_processing.v` 第 63–68 行，按 `COMMAND_PARAM=0`、`COMMAND_SEND_IMG=0+1=1` … 逐个算出硬件侧的数值。
3. 对照两侧，逐行检查同名命令的数值是否相等。

**需要观察的现象**：两侧 13 个命令（`COMMAND_PARAM` 到 `COMMAND_APPLY_MULT`）的数值完全一致，从 0 到 12。

**预期结果**：所有命令的数值两两相等。此外你会发现 C++ 多出的 `COMMAND_NONE=255` 在 HDL 的 parameter 列表里没有对应项——这是有意为之，因为它只是一个 C++ 内部的「空」标记。

#### 4.1.5 小练习与答案

**练习 1**：如果将来要新增一条命令 `COMMAND_APPLY_GAMMA`，应该插在枚举的哪个位置？插在中间和插在末尾，分别会有什么后果？

**参考答案**：必须**同时**在 C++ 枚举和 HDL parameter 两处、且**相同相对位置**插入，才能保证数值一致。若插在末尾（`COMMAND_APPLY_MULT` 之后，值为 13），不会打乱已有命令的数值，最安全。若插在中间，会导致其后所有命令的数值整体后移——只要软硬件两侧同步修改就没问题；但若只改了一侧，所有后续命令的解读都会错位，是典型的「契约被打破」bug。

**练习 2**：`COMMAND_NONE` 为什么选 255 而不是 13？

**参考答案**：255 远离正常命令区间（0~12），是一个明显的「非法/空」标记值。这样在调试时，一旦看到某个变量等于 255，就能立刻意识到「这里没有有效命令」，而不会和某条真实命令混淆。此外 255 是一个 8 位全 1 的值，在硬件总线上也容易识别。

---

### 4.2 Image_processing 纯虚接口：一套函数签名统一两套后端

#### 4.2.1 概念说明

光有操作码还不够。主机软件需要一个**入口**去发出这些命令——也就是一组函数。但这里有个矛盾：仿真后端发命令的方式（往一个 C++ `std::queue` 里 push）和硬件后端发命令的方式（通过 USB→FTDI→SPI 发字节）完全不同。

如果让 `main.cpp` 直接调用某个具体后端，那它就必须为仿真和硬件各写一份逻辑，项目就分裂成了两套主机程序。

解决办法是面向对象里的**依赖倒置**：定义一个**纯虚基类** `Image_processing`，它只声明「有哪些操作、参数是什么」，不关心「具体怎么发」。两套后端各自继承它、给出自己的实现。`main.cpp` 只持有一个**基类指针** `Image_processing *img_proc`，调用 `img_proc->send_add(...)` 时，C++ 会根据指针实际指向的对象，自动分派到仿真或硬件的实现。

这样，`main.cpp` 就只依赖「接口契约」，与底层通信方式彻底解耦。

#### 4.2.2 核心流程

基类里每一个 `virtual ... = 0` 的函数都是**纯虚函数**，意味着：

- 基类自己**不提供实现**（`= 0` 是语法标记）。
- 派生类**必须**实现它，否则无法实例化。

派生类（`Image_processing_simulation` / `Image_processing_ice40`）实现这些函数时，做的工作本质相同：**把高层调用翻译成一条 `COMMAND_*` 命令加上若干参数字节**。例如 `send_add(32, true)` 在两个后端里都被翻译成「发 `COMMAND_APPLY_ADD`，再发 2 字节的 value、1 字节的 clamp」，只是「发」的物理通道不同（FIFO 队列 vs SPI 总线）。

函数调用与硬件命令的分派关系可以这样概括（细节表见第 5 节综合实践）：

```
main.cpp 的高层调用          翻译成（两个后端都一样）         硬件 FSM 收到
─────────────────────────   ─────────────────────────────   ─────────────────
send_params(w, h)        →  COMMAND_PARAM + w(LE2) + h(LE2) → 初始化尺寸/缓冲
send_image(img)          →  COMMAND_SEND_IMG + 像素字节      → 载入 input 缓冲
send_add(v, clamp)       →  COMMAND_APPLY_ADD + v(LE2)+clamp→ 逐像素加法
wait_end_busy()          →  反复发 COMMAND_GET_STATUS        → 轮询 busy 位
...
```

#### 4.2.3 源码精读

基类定义在 `image_processing.hpp`：

[software/image_processing.hpp:8-10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8-L10) —— 声明 `class Image_processing`，有一个空的默认构造函数（派生类构造时会先调它）。

[software/image_processing.hpp:15-34](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L15-L34) —— 全部纯虚函数声明。每个 `= 0` 都是一条必须由后端实现的契约。

来看后端是怎么兑现契约的。以 `send_add` 为例，仿真后端：

[simulation/image_processing_simulation.cpp:72-83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L72-L83) —— `send_add` 把 value 拆成 2 个小端字节，往 `fifo_in` 里依次 push：命令 `COMMAND_APPLY_ADD`、低字节、高字节、clamp。

硬件后端做**完全相同的翻译**，只是 push 变成了「SPI 发送」：

[ice40/software/image_processing_ice40.cpp:90-97](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L90-L97) —— 同样发 `COMMAND_APPLY_ADD`、低字节、高字节、clamp，只不过通过 `spi_command_send` 走 SPI 总线。

> 这两段代码的关键启示：**两个后端发出的字节序列是一模一样的**。差别只在于「字节往哪儿送」。正是因为字节序列相同，同一份 HDL 状态机才能同时服务仿真和硬件两种场景。字节级打包的细节（小端序、位打包）将在下一讲 `u2-l2` 详讲。

#### 4.2.4 代码实践

**实践目标**：建立「虚函数 → COMMAND_*」的初步映射直觉。

**操作步骤**：

1. 打开 `image_processing.hpp` 第 15–34 行，列出全部 15 个纯虚函数名。
2. 对照仿真后端 `simulation/image_processing_simulation.cpp`，对每个函数找到它 push 的第一个 `Operation(true, COMMAND_XXX, 0)`，读出对应的 `COMMAND_*`。
3. 抽查 3 个函数（如 `send_image`、`switch_buffers`、`send_convolution`），再到硬件后端 `ice40/software/image_processing_ice40.cpp` 里确认它发的 `COMMAND_*` 是否相同。

**需要观察的现象**：每个虚函数内部都有且仅有一条命令（`COMMAND_*`）作为「操作码」，后跟若干参数字节；两个后端对同一函数发出的 `COMMAND_*` 完全一致。

**预期结果**：例如 `send_image` 两端都发 `COMMAND_SEND_IMG`，`switch_buffers` 两端都发 `COMMAND_SWITCH_BUFFERS`，`send_convolution` 两端都发 `COMMAND_CONVOLUTION`。完整的对照表见第 5 节。

#### 4.2.5 小练习与答案

**练习 1**：基类里为什么用纯虚函数（`= 0`）而不是普通虚函数（带默认实现）？

**参考答案**：因为基类**根本不知道**该怎么「发」一条命令——发往 FIFO 还是 SPI，取决于具体后端，没有合理的默认值可选。用纯虚函数可以**强制**每个派生类都给出自己的实现，编译期就能保证「没有任何后端会漏掉某个操作」。如果改成带默认实现的虚函数，某个后端忘了重写时，会静默继承一个错误的实现，bug 难以发现。

**练习 2**：`main.cpp` 里的 `test_*` 函数签名里，第三个参数是 `Image_processing *img_proc`（基类指针）。为什么不是 `Image_processing_simulation *`？

**参考答案**：用基类指针类型，`test_*` 函数就能接受**任何**派生类对象——仿真后端、硬件后端、甚至将来新增的后端。这是「针对接口编程，而非针对实现编程」的直接体现，也是同一份 `main.cpp` 能驱动两套后端的根本原因。

---

### 4.3 受保护成员 image_width / image_height：状态由基类托管

#### 4.3.1 概念说明

主机在 `send_params(image_width, image_height)` 里告诉后端图像的宽高后，**后续很多操作都要复用这两个值**：`send_image` 要知道发多少个像素、`read_image` 要知道读多少个、`wait_end_busy` 也间接依赖尺寸（大图运算更慢）。

如果每次调用都要重新传宽高，接口会很啰嗦；如果每个后端各自存一份，又容易不一致。项目的做法是：把 `image_width`、`image_height` 作为基类的 **`protected` 成员**，由基类统一托管，派生类可以直接读写。

`protected` 的含义是：**对外（外部代码）是私有的，对派生类是公开的**。这样既封装了状态（`main.cpp` 不能随便改它），又让两个后端能方便地访问。

#### 4.3.2 核心流程

```
send_params(w, h) 被调用
        │
        ├── 把 w、h 存进基类的 image_width、image_height   ← 状态「落地」一次
        │
        └── 后续 send_image / read_image 直接读这两个成员    ← 处处复用
            （循环 image_width*image_height 次）
```

这是一个典型的「**一次声明，处处复用**」模式：状态在最入口处设置一次，之后整个对象生命周期内都生效。

#### 4.3.3 源码精读

基类声明这两个受保护成员：

[software/image_processing.hpp:36-38](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L36-L38) —— `protected` 区段里的 `image_width`、`image_height`，类型为 `uint16_t`（最大支持 65535×65535，远超实际图像）。

派生类在 `send_params` 里写入它们。仿真后端：

[simulation/image_processing_simulation.cpp:18-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L18-L22) —— `send_params` 开头先 `this->image_width = img_width; this->image_height = img_height;` 把入参存进基类成员。

之后 `send_image` 直接用基类成员做循环边界：

[simulation/image_processing_simulation.cpp:61-70](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L61-L70) —— `send_image` 循环 `image_width*image_height` 次把像素 push 进队列，完全依赖基类里存的尺寸。

> 注意：`send_params` 同时做了两件事——**保存尺寸到基类成员**（C++ 侧的状态），以及**把尺寸打包成字节发给硬件**（硬件侧的状态）。两者不能混淆：前者是主机后端对象自己的记忆，后者是 FPGA 内部寄存器的值。下一讲 `u2-l2` 会讲后者的字节打包。

#### 4.3.4 代码实践

**实践目标**：追踪 `image_width/image_height` 的「写入点」和「读取点」。

**操作步骤**：

1. 在 `image_processing.hpp` 找到 `image_width`/`image_height` 的声明（36–38 行）。
2. 在仿真后端 `.cpp` 里搜索 `this->image_width` 和 `this->image_height`，确认它们只在 `send_params` 里被赋值。
3. 再搜索裸的 `image_width`、`image_height`（不带 `this->`），看 `send_image`、`read_image`、`read_image` 的循环如何引用它们。

**需要观察的现象**：这两个成员只在 `send_params` 中被写入一次，却在 `send_image`、`read_image` 等多处被读取，作为循环边界。

**预期结果**：例如 `send_image` 第 63、67 行的 `image_width*image_height`、`read_image` 第 154、158 行的同名表达式，都是在复用基类成员。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `protected` 改成 `private`，会怎样？

**参考答案**：派生类（两个后端）就无法直接读写 `image_width`/`image_height`，编译会报错。要么得在基类加 `get/set` 方法来访问，要么得在每个派生类里各自再声明一份同名成员——后者会导致状态分散、容易不一致。`protected` 正是为「基类托管状态、派生类自由使用」这种场景设计的。

**练习 2**：为什么 `send_image` 不再接收一个 `width/height` 参数，而是直接用基类成员？

**参考答案**：因为尺寸在 `send_params` 时已经「全局」确定，每次调用都重传是冗余且易错的（万一传错就会和硬件侧记录的尺寸不符）。用基类成员托管，既保证了「尺寸只在一处定义」，也简化了所有依赖尺寸的函数签名。

---

### 4.4 多态后端选择（#ifdef）：同一份 main.cpp 编出两个程序

#### 4.4.1 概念说明

有了基类和派生类，剩下的关键问题是：**`main.cpp` 怎么决定实例化哪个后端？**

答案是用预处理宏 `#ifdef`。在编译时，通过命令行传入 `-DSIMULATION` 或 `-DICE40`（见 `u1-l3`、`u1-l4`），`main.cpp` 里的 `#ifdef` 分支会**在编译期**决定 include 哪个后端头文件、`new` 出哪个后端对象。

注意这是**编译期选择**，不是运行期：编出来的可执行文件要么是仿真程序，要么是硬件程序，二选一。但 `main.cpp` 的**源码只有一份**，所有 `test_*` 函数、主流程都完全共享，只是后端对象的类型不同。这正是「一份源码，两个程序」的实现机制。

#### 4.4.2 核心流程

`main.cpp` 里有**两处** `#ifdef`，分工明确：

```
① 选头文件（编译期决定能看到哪个后端类的声明）
   #ifdef SIMULATION
       #include "../simulation/image_processing_simulation.hpp"   ← 仿真后端类
   #elif ICE40
       #include "../ice40/software/image_processing_ice40.hpp"    ← 硬件后端类
   #endif

② 选对象（编译期决定 new 出哪个后端实例）
   Image_processing *img_proc;                                    ← 基类指针（两份程序都有）
   #ifdef SIMULATION
       img_proc = new Image_processing_simulation();              ← 仿真：指针指向仿真对象
   #elif ICE40
       img_proc = new Image_processing_ice40();                   ← 硬件：指针指向硬件对象
   #endif

之后所有 img_proc->send_xxx(...) 调用，由 C++ 多态自动分派到对应后端。
```

两处 `#ifdef` 必须**配套**：第①处保证编译器「认识」后端类的类型，第②处保证「实例化」该后端。如果只改一处，编译或运行会出错。

#### 4.4.3 源码精读

`main.cpp` 顶部的头文件选择：

[software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) —— 根据 `SIMULATION`/`ICE40` 宏 include 对应后端的头文件。

`main()` 里实例化后端对象：

[software/main.cpp:224-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L224-L230) —— 先声明基类指针 `Image_processing *img_proc`，再用 `#ifdef` 决定 `new` 哪个派生类对象赋给它。

一旦指针指向了具体后端，下面所有 `test_*` 调用都自动走多态分派。以 `test_add_threshold` 为例：

[software/main.cpp:38-73](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L38-L73) —— 整个函数只通过基类指针 `img_proc` 调用接口（`send_params`、`switch_buffers`、`send_add`、`wait_end_busy`、`send_threshold`、`read_image`），完全不关心后端是仿真还是硬件——这就是契约带来的解耦威力。

> 一个容易混淆的点：`#ifdef` 是**预处理指令**，发生在编译之前。它不是 C++ 语言层面的多态，而是「在编译前裁剪源码」。而 `img_proc->send_add(...)` 的运行期分派才是真正的 C++ 多态。两者配合：`#ifdef` 决定「装哪个引擎」，多态决定「调用时引擎怎么响应」。

#### 4.4.4 代码实践

**实践目标**：理解「同一份 test 函数，两种后端行为」是如何实现的。

**操作步骤**：

1. 读 `main.cpp` 第 14–18 行和第 224–230 行，确认两处 `#ifdef` 的配套关系。
2. 读 `test_add_threshold`（第 38–73 行），数一数它调用了哪些基类接口。
3. 假想两次编译：一次带 `-DSIMULATION`、一次带 `-DICE40`。在脑中分别替换第②处的 `new`，思考同一个 `img_proc->send_add(32, true)` 调用会分别进入哪个后端的实现。

**需要观察的现象**：`test_add_threshold` 的源码里**没有任何** `#ifdef`、也没有出现 `simulation` 或 `ice40` 字样——它对后端类型完全无感。

**预期结果**：用 `-DSIMULATION` 编译时，`send_add` 进入 `Image_processing_simulation::send_add`（push 到 FIFO）；用 `-DICE40` 编译时，进入 `Image_processing_ice40::send_add`（SPI 发送）。源码不变，行为随编译宏切换。

#### 4.4.5 小练习与答案

**练习 1**：如果既没有定义 `SIMULATION` 也没有定义 `ICE40`，`main.cpp` 能编译通过吗？

**参考答案**：不能正常通过。第①处 `#ifdef`/`#elif` 都不命中，意味着没有任何后端头文件被 include，于是 `Image_processing_simulation` 和 `Image_processing_ice40` 两个类型名都不存在；第②处的 `new` 会因「未知类型」报错。这正是 `build_simulation.sh` 用 `-DSIMULATION`、`build_ice40.sh` 用 `-DICE40` 强制注入宏的原因。

**练习 2**：为什么不在运行时通过一个变量（如 `bool use_simulation`）来选后端，而要用编译期宏？

**参考答案**：因为两个后端的依赖差异巨大——仿真后端依赖 Verilator 生成的 `Vimage_processing` 类（只在装了 Verilator 的开发机上才有意义），硬件后端依赖 FTDI 的 `-lftdi` 库和真实 USB 硬件。把它们编译进同一个可执行文件既浪费又会引入不必要的依赖。用编译期宏裁剪，可以让每个产物只携带自己需要的代码和依赖，产物更小、部署更清晰。

---

### 4.5 send_clear 的复用案例：接口设计的威力

#### 4.5.1 概念说明

`send_clear(uint8_t value)` 的作用是「把整个 storage 缓冲填满为同一个值 `value`」。它看起来应该是一条独立的「清屏」命令。

但如果你打开两个后端的实现，会发现 `send_clear` **根本没有发任何新命令**，而是直接调用了 `send_threshold(0, value, true)`：

```cpp
void send_clear(uint8_t value){
   this->send_threshold(0, value, true);
}
```

这是一个非常巧妙的**接口复用**：它利用了「阈值运算」的一个边界条件，用一个已有命令实现了看似不同的功能，从而**不需要在硬件侧新增任何命令或状态**。这个案例完美展示了「把硬件复杂度藏进接口设计」的思想。

#### 4.5.2 核心流程

要理解这个复用，得先看阈值运算的语义。`send_threshold(threshold_value, replacement, upper_selection)` 在硬件里的行为是：

- 当 `upper_selection == 1`（真）：把所有 **大于等于** `threshold_value` 的像素，替换成 `replacement`。
- 当 `upper_selection == 0`（假）：把所有 **小于等于** `threshold_value` 的像素，替换成 `replacement`。

那么 `send_threshold(0, value, true)` 的含义就是：

```
threshold_value    = 0
replacement        = value
upper_selection    = 1（真）→ 选中「像素 >= 0」的那些

由于像素是无符号 8 位（范围恒为 0~255），
「像素 >= 0」对每个像素都成立！

⇒ 每一个像素都被替换成 value
⇒ 整幅图被填满为 value   ← 这正是 send_clear 想要的效果
```

也就是说，「清屏」=「用一个阈值为 0 的上界阈值，把所有像素（因为全都 ≥0）替换成目标值」。数学上：

\[
\forall p \in [0,255],\quad p \ge 0 \;\Longrightarrow\; p \leftarrow \text{value}
\]

由于无符号像素 \(p\) 恒满足 \(p \ge 0\)，所以替换无条件成立。

#### 4.5.3 源码精读

仿真后端的 `send_clear` 实现：

[simulation/image_processing_simulation.cpp:217-219](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L217-L219) —— `send_clear` 直接转调 `send_threshold(0, value, true)`，不发任何独立命令。

硬件后端做了一模一样的复用：

[ice40/software/image_processing_ice40.cpp:209-211](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L209-L211) —— 同样转调 `send_threshold(0, value, true)`。

复用之所以成立，依赖硬件侧阈值运算的判定逻辑：

[hdl/image_processing.v:521-537](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L521-L537) —— 阈值运算核心：`threshold_upper==1` 时判定 `data_read >= threshold_value` 才替换。当 `threshold_value==0`，`data_read >= 0` 对无符号像素恒真，于是全部像素被替换为 `threshold_replacement`。

> 这个案例的深层启示：**好的接口设计能减少硬件负担**。如果为「清屏」单开一条命令，就要在 `Commands` 枚举、两套后端、HDL 状态机里各加一套分支，维护成本陡增。而通过洞察「清屏是阈值为 0 的阈值的特例」，用一个边界条件就复用了全部已有机制。设计接口时多问一句「这个新功能是不是某个已有功能的特例」，往往能省下大量代码。

#### 4.5.4 代码实践

**实践目标**：验证 `send_clear` 的复用在真实测试中确实产生「整图填满」的效果。

**操作步骤**：

1. 读 `main.cpp` 中的 `test_binary_add`（第 77–91 行），它先用 `send_clear(32)` 把 storage 缓冲填满 32，再与 input 缓冲做加法。
2. 在脑中把 `send_clear(32)` 展开成 `send_threshold(0, 32, true)`，按第 4.5.2 节的推导，确认每个像素都会变成 32。
3. （可选，待本地验证）在仿真模式下运行 `test_binary_add`，观察 storage 被清成 32 后，与 input 相加的结果是否等于「input 每个像素 + 32」。

**需要观察的现象**：`test_binary_add` 不需要任何「清屏专用命令」就能让 storage 全部变为 32——这完全靠阈值的边界条件实现。

**预期结果**：storage 缓冲被填满常量 32，随后 `send_binary_add(true)` 把 input 与这个常量图相加并钳位，等价于「input 每像素 + 32」。

**待本地验证**：第 3 步若你本地装了 Verilator，可用 `build_simulation.sh` 编译、把 `main.cpp` 第 256 行的 `test_simple_edge_detection` 换成 `test_binary_add` 再运行，用 `run_gnuplot.sh` 查看输出图是否符合「原图整体变亮 32」的预期。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `send_clear` 改成 `send_threshold(0, value, false)`（`upper_selection` 改成假），还能正确清屏吗？

**参考答案**：不能。`upper_selection==0` 时判定条件变成「像素 <= threshold_value」。当 `threshold_value==0` 时，只有**恰好等于 0** 的像素才会被替换（因为 `<= 0` 等价于 `== 0`，无符号数不可能小于 0）。于是只有原本是 0 的像素变成 `value`，其余像素不变，达不到「整图填满」的效果。这也反过来说明 `true` 是必须的。

**练习 2**：能否用 `send_threshold(255, value, false)` 实现清屏？为什么？

**参考答案**：可以。`upper_selection==0`、`threshold_value==255` 时，判定条件是「像素 <= 255」，而无符号像素最大就是 255，所以该条件对**所有像素**恒真，同样能把整图替换为 `value`。这是「清屏」的另一个边界条件写法，与 `send_threshold(0, value, true)` 等价。项目选 `(0, true)` 这一种，只是约定。

---

## 5. 综合实践

把本讲的全部知识串起来，完成下面这份「契约速查表」——它是你今后阅读任何后端代码时的索引。

### 实践任务

打开 `software/image_processing.hpp` 与两个后端 `.cpp`，把每个纯虚函数填进下表的空缺处。每行要标注：**发送的 `COMMAND_*` 及其数值、参数含义、是否需要在调用后 `wait_end_busy`**。最后解释 `send_clear` 为何是 `send_threshold(0, value, true)` 的复用。

### 参考答案表

下表是基于源码核实后的完整映射（`COMMAND_*` 数值来自 `hdl/image_processing.v:63-68`，参数与等待关系来自两个后端 `.cpp` 及 `main.cpp` 各 `test_*` 的实际用法）：

| 纯虚函数 | 发送的 `COMMAND_*` (值) | 参数含义（小端字节，详见 u2-l2） | 需 `wait_end_busy`？ |
| --- | --- | --- | --- |
| `send_params(w, h)` | `COMMAND_PARAM` (0) | 宽 2 字节 + 高 2 字节 | 否（仅设尺寸） |
| `send_image(img)` | `COMMAND_SEND_IMG` (1) | `w*h` 个像素字节 | 否（仅载入） |
| `read_image(img)` | `COMMAND_READ_IMG` (2) | 无（回读 `w*h` 个字节） | 否（读取本身即同步） |
| `read_status(out)` | `COMMAND_GET_STATUS` (3) | 无（回读 4 字节状态） | 否 |
| `send_add(value, clamp)` | `COMMAND_APPLY_ADD` (4) | value 2 字节 + clamp 1 字节 | **是** |
| `send_threshold(thr, repl, upper)` | `COMMAND_APPLY_THRESHOLD` (5) | thr + repl + upper 各 1 字节 | **是** |
| `switch_buffers()` | `COMMAND_SWITCH_BUFFERS` (6) | 无 | 否（仅交换地址寄存器） |
| `send_binary_add(clamp)` | `COMMAND_BINARY_ADD` (7) | clamp 1 字节 | **是** |
| `send_image_invert()` | `COMMAND_APPLY_INVERT` (8) | 无 | **是** |
| `send_convolution(kernel, clamp, src, add)` | `COMMAND_CONVOLUTION` (9) | `(add<<2)+(src<<1)+clamp` 1 字节 + 9 个 kernel 字节 | **是** |
| `send_binary_sub(clamp, abs_diff)` | `COMMAND_BINARY_SUB` (10) | `(abs_diff<<1)+clamp` 1 字节 | **是** |
| `send_binary_mult(clamp)` | `COMMAND_BINARY_MULT` (11) | clamp 1 字节 | **是** |
| `send_mult(value, clamp)` | `COMMAND_APPLY_MULT` (12) | value 的定点字节 + clamp | **是** |
| `send_clear(value)` | 复用 `COMMAND_APPLY_THRESHOLD` (5) | = `send_threshold(0, value, true)`：thr=0, repl=value, upper=1 | **是** |
| `wait_end_busy()` | 反复 `COMMAND_GET_STATUS` (3) | 无（轮询 busy 位直到为 0） | （它本身就是「等待」） |

> 判断「是否需要 `wait_end_busy`」的规则很简单：**凡是会触发硬件实际运算的命令（unary/binary/卷积/清屏），都要等**；而**只搬运数据或交换缓冲的命令（参数、收发图、切缓冲、查状态），不用等**。`main.cpp` 里每个 `test_*` 的写法都印证了这一点——`send_add` 之后必跟 `wait_end_busy`，而 `send_image` 之后从不跟。

### `send_clear` 复用的解释

`send_clear(value)` 不发新命令，而是调用 `send_threshold(0, value, true)`。因为阈值运算在 `upper_selection=true` 时替换所有 `>= threshold_value` 的像素；当 `threshold_value=0` 时，无符号像素恒满足 `>= 0`，于是**每一个像素**都被替换为 `value`，整幅图被填满为同一值——这正是「清屏」的语义。通过洞察「清屏是阈值为 0 的阈值的特例」，项目用一条已有命令省去了硬件侧的新增分支。详见第 4.5 节。

## 6. 本讲小结

- `image_processing.hpp` 用一个 `Commands` 枚举 + 一个 `Image_processing` 纯虚基类，定义了**主机软件、两套后端、HDL 模块四方共同遵守的契约**，是全项目最薄却最关键的文件。
- `Commands` 枚举的数值（0~12）与 `hdl/image_processing.v` 的 `parameter COMMAND_*` **逐字对应**，这是软件字节流能被硬件正确解读的根本保证；新增命令必须两侧同步。
- `Image_processing` 的每个纯虚函数都映射到一条 `COMMAND_*`；两套后端发出的**字节序列完全相同**，差别只在「字节往哪儿送」（FIFO vs SPI）。
- `image_width/image_height` 作为 `protected` 成员由基类托管，在 `send_params` 写入一次，处处复用，避免了状态分散。
- `main.cpp` 用两处配套的 `#ifdef SIMULATION / ICE40` 在**编译期**选择后端，再用 C++ **多态**在运行期分派调用，实现「一份源码，两个程序」。
- `send_clear` 复用 `send_threshold(0, value, true)`，展示了「把硬件复杂度藏进接口设计」的思想——好的接口能用已有机制的边界条件省下整条新命令。

## 7. 下一步学习建议

本讲只建立了「函数 → 命令」的**符号级**映射，但还没有打开「参数字节是怎么打包的」这个黑盒。建议下一步学习：

- **`u2-l2 命令协议与报文格式`**：深入讲解「1 字节操作码 + 变长小端参数」的报文结构，以及 `(absolute_diff<<1)+clamp`、`(add_to_output<<2)+(input_source<<1)+clamp` 这类**位打包**技巧，并对照仿真/ice40 两端验证字节序列一致。
- 之后可进入 **`u2-l3 图像数据格式`**，了解 `.h` 图像如何被 `HEADER_PIXEL` 宏解包成灰度像素。

如果你更想先看硬件侧，也可以跳到 **`u3-l3 主命令处理状态机`**，看 `STATE_WAIT_COMMAND` 如何根据 `comm_cmd` 把本讲这些 `COMMAND_*` 派发到对应的处理状态——那是这份契约在 FPGA 内部的「兑现现场」。
