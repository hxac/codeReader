# 模拟器设备与外设仿真

## 1. 本讲目标

上一讲（u8-l1）我们读完了 Nyuzi 的 C 指令集模拟器如何取指、派发、执行指令，并把架构状态（寄存器、内存、PC）维护起来。但一台能跑真实软件的「机器」光有 CPU 还不够——程序要往屏幕画图、要从键盘读输入、要从「硬盘」读文件。在真实 FPGA 板上，这些由 VGA 控制器、PS/2 键盘控制器、SD 卡控制器等外设硬件完成；在模拟器里，则由宿主机（你的电脑）上的普通 C 代码「假装」出来。

本讲要回答四个问题：

1. 模拟器怎么知道一条访存指令是「访问外设」而不是「访问内存」？它又怎么把这次访问分发给正确的外设？——**设备分发**
2. 程序往帧缓冲（framebuffer）里画的像素，是怎么变成宿主机屏幕上一个窗口里的画面的？——**帧缓冲**
3. 模拟器怎么用宿主机上的一个普通文件，假装成一张可读写的 SD 卡？——**虚拟块设备**
4. 程序里的 `printf` 是怎么跑到你终端上的？——**UART 串口**

学完本讲，你应当能：画出一次外设寄存器访问从「指令」到「宿主副作用」的完整调用链；解释帧缓冲地址如何映射到 SDL 窗口；读懂 SD/MMC 的 SPI 状态机；并用 `-f`、`-b` 等命令行选项驱动这些仿真外设。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个概念。

**内存映射 I/O（MMIO）。** Nyuzi 没有专门的「端口输入输出」指令（如 x86 的 `in`/`out`）。它把一部分**地址空间**留给外设：任何落在 `0xffff0000` 及以上的地址，都不对应真实 RAM，而对应某个外设里的一个 32 位寄存器。于是「写地址 `0xffff0048`」就等价于「往串口发一个字节」。CPU 用普通的 `load_32`/`store_32` 指令访问它们，模拟器和 FPGA 用同一套地址表，所以**同一份软件可以不改地在两种环境上运行**。

**外设寄存器。** 一个外设通常暴露几个寄存器：状态寄存器（有没有数据可读）、数据寄存器（读出/写入一个字节）、控制寄存器（开/关、片选）。软件靠轮询状态位或靠中断来知道「外设有事发生」。

**帧缓冲（framebuffer）。** 一块连续内存，每个像素占若干字节（Nyuzi 用 32 位/像素，ABGR 顺序）。软件往这块内存写颜色，显示设备按行扫描把它变成画面。本讲里这块内存就在模拟器的「模拟内存」数组里，地址通常是 `0x200000`。

**SDL。** Simple DirectMedia Layer，一个跨平台 C 库，能在宿主机上开窗口、画纹理、收键盘事件。模拟器用它把上面的帧缓冲「贴」到一个真实窗口上。

**SPI 与 SD 卡。** SD 卡可以工作在 SPI 模式下：主机拉低片选（CS），然后按字节双向交换数据（主机发一字节、从机回一字节）。读写以「块」（block，默认 512 字节）为单位，靠 6 字节的命令帧驱动。模拟器用一个状态机逐字节地「扮演」这张卡。

**中断。** 外设事件（串口收到一个字符、键盘按了一下、一帧画完）可以变成对 CPU 的中断。模拟器用 `raise_interrupt` / `clear_interrupt` 设置一个挂起位图，下一拍让目标线程跳进中断处理。中断号用位掩码表示（如 `INT_UART_RX = 0x4`）。

> 承接 u8-l1：模拟器只维护**架构状态**、不建模微架构。本讲的外设同样是「功能级」仿真——它逼真地**表现**出一张 SD 卡、一个串口、一块屏幕，但并不逐时钟周期地复刻 FPGA 上的真实硬件时序。

## 3. 本讲源码地图

本讲全部源码都在 `tools/emulator/` 目录下，外加软件侧的对应头文件：

| 文件 | 作用 |
|------|------|
| [tools/emulator/device.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c) | **外设分发中枢**。把一次设备寄存器读/写 switch 到具体外设（串口、SD、VGA、键盘），并维护键盘/串口输入的环形缓冲。 |
| [tools/emulator/device.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.h) | 设备寄存器地址表 `REG_*`、`DEVICE_BASE_ADDRESS`、中断位掩码 `INT_*`、对外接口声明。 |
| [tools/emulator/fbwindow.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/fbwindow.c) | **帧缓冲窗口**。用 SDL 开窗口、把模拟内存里的一块帧缓冲贴成纹理，并把宿主键盘事件翻译成 PS/2 扫描码。 |
| [tools/emulator/sdmmc.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c) | **虚拟 SD 卡**。用宿主文件作后端，按 SPI 字节流实现 SD 命令状态机（读块/写块）。 |
| [tools/emulator/main.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c) | 命令行解析（`-f`/`-b`/`-r` 等）、主循环里周期性刷新帧缓冲与轮询输入、宿主中断管道。 |
| [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | **访存路由**。在执行 load/store 时判断地址是否落在设备区，并调用 `read/write_device_register`。 |
| software/libs/libos/bare-metal/[registers.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h) | **软件侧**的寄存器名表（`REG_UART_TX` 等），与 device.h 的绝对地址一一对应，是「同一套硬件、两种命名」的桥梁。 |
| software/libs/libos/bare-metal/[vga.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c) | 软件侧初始化 VGA 的代码，写 `REG_VGA_BASE`/`REG_VGA_ENABLE`——这正是设备分发要拦截的写操作。 |

> 注意命名差异：软件里叫 `REG_UART_TX`，模拟器里叫 `REG_SERIAL_OUTPUT`，两者**字节地址都是 `0xffff0048`**，是同一根寄存器的两个名字。

## 4. 核心概念与源码讲解

### 4.1 设备分发：一次访存如何路由到外设

#### 4.1.1 概念说明

CPU 看到的内存是一块平坦地址空间。模拟器需要在这一块空间里划出一道线：线以下是真 RAM（模拟内存数组），线以上是外设寄存器。这道线就是 `DEVICE_BASE_ADDRESS = 0xffff0000`。

「设备分发」要做两件事：

- 在执行 load/store 时，**判断**物理地址是否 `>= 0xffff0000`；
- 若是，则**不再读写内存数组**，而是把地址交给 `read_device_register` / `write_device_register`，由它们用一个 `switch` 把地址对应到具体外设的处理逻辑。

之所以要单独成一层，是因为外设种类多、行为各异（有的只读、有的只写、有的有副作用），集中在一个 switch 里分发，既清晰又便于增删外设。

#### 4.1.2 核心流程

一次 32 位字访存的分发流程（伪代码）：

```
translate_address(va) -> pa              # 虚拟地址翻译成物理地址
if pa >= 0xffff0000:                     # 落在设备区
    要求访存宽度必须是 MEM_LONG (32 位)    # 否则报 "Invalid device access"
    if 是 load:
        value = read_device_register(pa)  # 进 switch 找外设
    else:                                 # 是 store
        if pa == REG_TIMER_INT: 定时器特殊处理
        else: write_device_register(pa, value)
    直接返回，跳过缓存/日志等副作用
else:
    正常读写模拟内存数组 memory[pa]
```

注意三个边界：**块访存**（`loadv`/`storev` 整个向量）和 **scatter/gather** 访问设备地址会被判为非法并使模拟器崩溃（`crashed = true`），因为外设不支持成块/逐通道访问；**非 32 位**的设备访问（字节/半字）只会打印一条调试告警。

#### 4.1.3 源码精读

**地址表与基地址。** `DEVICE_BASE_ADDRESS` 与全部 `REG_*` 绝对地址定义在 device.h：

[tools/emulator/device.h:22-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.h#L22-L37) 定义了 `DEVICE_BASE_ADDRESS 0xffff0000` 以及各外设寄存器的绝对地址（如 `REG_SERIAL_OUTPUT 0xffff0048`、`REG_VGA_BASE 0xffff0188`、`REG_SD_CONTROL 0xffff00cc`）。下面的 `INT_*` 是中断位掩码。

**访存路由。** processor.c 在 load 路径里先翻译地址、再判定设备区、最后要求 32 位字访问：

[tools/emulator/processor.c:1277-1300](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1277-L1300) 这段先调用 `translate_address` 得到物理地址，置 `is_device_access = physical_address >= DEVICE_BASE_ADDRESS`；若设备访问却不是 `MEM_LONG` 则打印告警；`MEM_LONG` 的 load 在设备区分支调用 `read_device_register(physical_address)`，否则读模拟内存。

store 路径几乎对称，但**定时器寄存器被特判**（写它只是设置 `current_timer_count`，不走通用分发）：

[tools/emulator/processor.c:1358-1371](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1358-L1371) `MEM_LONG` 的 store 在设备区分支里：若地址是 `REG_TIMER_INT` 则更新定时器计数，否则调用 `write_device_register`，随后 `return` 跳过后续日志。

非法的块/scatter 设备访问（崩溃）：

[tools/emulator/processor.c:1468-1474](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1468-L1474) 块向量访存一旦落在设备区，打印 "Illegal block access to device address" 并置 `crashed = true`。（scatter 路径在 1555-1561 行有对称处理。）

**写分发 switch。** device.c 的 `write_device_register` 把地址映射到外设动作：

[tools/emulator/device.c:42-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L42-L71) switch 各 case：`REG_SERIAL_OUTPUT`→`putc` 到 stdout（UART，见 4.4）；`REG_SD_WRITE_DATA`→`transfer_sdmmc_byte`（SD，见 4.3）；`REG_SD_CONTROL`→`set_sdmmc_cs`（SD 片选）；`REG_VGA_ENABLE`→`enable_frame_buffer`（帧缓冲，见 4.2）；`REG_VGA_BASE`→`set_frame_buffer_address`；`REG_HOST_INTERRUPT`→`send_host_interrupt`（宿主中断管道）。**没有 case 的地址（如 `REG_VGA_MICROCODE`、LED 等）落到 switch 末尾，被静默忽略。**

**读分发 switch。** `read_device_register` 类似，返回外设状态/数据：

[tools/emulator/device.c:73-127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L73-L127) 各 case 返回串口状态/输入、键盘状态/扫描码、SD 回读字节、SD 状态；`default` 返回 `0xffffffff`（读一个未实现寄存器得到全 1）。

#### 4.1.4 代码实践

1. **目标**：验证「同一根寄存器、两种命名」并定位分发路径。
2. **步骤**：
   - 在 [registers.h:31-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L31-L33) 找到 `REG_UART_TX = 0x0048 / 4`，结合第 21 行 `REGISTERS = (unsigned int*) 0xffff0000`，算出软件写 `REGISTERS[REG_UART_TX]` 的**字节地址** = `0xffff0000 + 0x12*4 = 0xffff0048`。
   - 在 [device.h:28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.h#L28) 确认 `REG_SERIAL_OUTPUT 0xffff0048` 与之相等。
   - 在 [processor.c:1280](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1280) 与 [device.c:46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L46) 之间画出调用链：`store_32(0xffff0048)` → `translate_address` → `write_device_register(0xffff0048, v)` → `putc(v, stdout)`。
3. **观察**：这条链说明软件的一次内存写，最终变成宿主终端上一个字符。
4. **预期结果**：能写出「`REGISTERS[REG_UART_TX]='A'` ⇒ 宿主 stdout 打印 A」的端到端解释。
5. 命令行运行结果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么块向量访存（`storev`）落在 `0xffff0000` 以上会让模拟器崩溃，而 `store_32` 不会？
**答**：设备寄存器按 32 位字编排、且多有副作用，不支持一次搬运 64 字节或逐通道 scatter。processor.c 在块/scatter 路径显式判定设备地址并置 `crashed = true`；`store_32` 则被 `MEM_LONG` 分支正常分发。

**练习 2**：软件往 `REG_VGA_MICROCODE`（`0xffff0184`）写了很多视频时序微码，模拟器会因此改变画面时序吗？
**答**：不会。`write_device_register` 的 switch 没有 `REG_VGA_MICROCODE` 的 case，这些写被静默忽略；模拟器用 SDL 窗口＋刷新率代替真实 VGA 时序（详见 4.2）。

---

### 4.2 帧缓冲：模拟内存如何变成宿主窗口画面

#### 4.2.1 概念说明

在 FPGA 上，VGA 控制器是一块独立硬件：软件往它里面写一段「微码」来编程水平/垂直同步时序（前廊、同步脉冲、后廊、可见行数），它再按像素时钟扫描帧缓冲、产生 `hsync`/`vsync` 信号驱动显示器。

模拟器**故意不仿真这套时序硬件**——那既慢又对软件功能无意义。它只关心两件事：

- **帧缓冲在模拟内存的哪里？** → 软件 写 `REG_VGA_BASE` 告诉它（通常是 `0x200000`）。
- **要不要显示？** → 软件写 `REG_VGA_ENABLE`（1=开、0=关）。

拿到这两个信息后，模拟器每隔若干条指令，把那块模拟内存**整块拷贝**到一个 SDL 纹理上并呈现到窗口。本质上是用「宿主机的显卡」替代了「被仿真的 VGA 控制器」。

#### 4.2.2 核心流程

```
软件侧 init_vga():                        # vga.c
   写 REG_VGA_ENABLE = 0                   # 被模拟器忽略
   写一堆 REG_VGA_MICROCODE ...            # 被模拟器忽略
   写 REG_VGA_BASE = 0x200000  ──┐         # set_frame_buffer_address 记下 fb_address
   写 REG_VGA_ENABLE = 1  ────────┤         # enable_frame_buffer 置 fb_enabled=true
                                  │
模拟器主循环 (main.c, fb 模式):   │
   execute_instructions(screen_refresh_rate 条)   # 让程序跑一阵、往帧缓冲写像素
   update_frame_buffer(proc):     │
      if !fb_enabled: return      │
      ptr = get_memory_region_ptr(proc, fb_address, w*h*4)  # 取出那块模拟内存
      SDL_UpdateTexture(纹理, ptr)  # 上传为纹理
      SDL_RenderCopy + Present    # 贴到窗口
      raise INT_VGA_FRAME; clear INT_VGA_FRAME   # 一拍脉冲
   poll_fb_window_event()         # 顺便收宿主键盘事件（见下文）
```

帧缓冲的字节大小为 \( \text{width} \times \text{height} \times 4 \)（每像素 4 字节，`SDL_PIXELFORMAT_ABGR8888`）。

#### 4.2.3 源码精读

**软件侧触发。** vga.c 在 `compile_microcode` 末尾写出模拟器真正关心的两个寄存器：

[software/libs/libos/bare-metal/vga.c:127-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c#L127-L129) `REGISTERS[REG_VGA_BASE] = FB_BASE`（`FB_BASE` 在第 20 行定义为 `0x200000`）与 `REGISTERS[REG_VGA_ENABLE] = 1`。其上方的微码写入（`REG_VGA_MICROCODE`）在模拟器里被忽略。

**模拟器侧记录。** device.c 把这两个写转成 fbwindow 的状态：

[tools/emulator/device.c:59-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L59-L65) `REG_VGA_ENABLE`→`enable_frame_buffer(value & 1)`，`REG_VGA_BASE`→`set_frame_buffer_address(value)`。

**SDL 窗口与纹理。** fbwindow.c 在启动时建好窗口和一张可流式更新的纹理：

[tools/emulator/fbwindow.c:31-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/fbwindow.c#L31-L67) `init_frame_buffer` 调 `SDL_Init`、建窗口、建 renderer，并用 `SDL_CreateTexture(... SDL_PIXELFORMAT_ABGR8888, SDL_TEXTUREACCESS_STREAMING, width, height)` 建帧缓冲纹理。

**刷新：把模拟内存贴成画面。** `update_frame_buffer` 是核心：

[tools/emulator/fbwindow.c:328-349](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/fbwindow.c#L328-L349) 若 `fb_enabled` 为假直接返回；否则用 `get_memory_region_ptr(proc, fb_address, fb_width*fb_height*4)` 从模拟内存取出帧缓冲指针，`SDL_UpdateTexture` 上传、`SDL_RenderCopy` + `SDL_RenderPresent` 显示，最后 `raise_interrupt(INT_VGA_FRAME)` 紧接 `clear_interrupt`（一次一拍脉冲）。

**主循环驱动刷新。** main.c 在开了 `-f` 时按刷新率周期调用上述函数：

[tools/emulator/main.c:405-413](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L405-L413) `while (execute_instructions(proc, screen_refresh_rate))` 每跑 `screen_refresh_rate` 条指令就 `update_frame_buffer`、`poll_fb_window_event`、`poll_inputs`。默认刷新率见 [fbwindow.c:29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/fbwindow.c#L29)（`screen_refresh_rate = 500000`），可用 `-r` 改。

> 附带能力：fbwindow.c 还把**宿主键盘**翻译成 PS/2 扫描码喂给软件。`sdl_to_ps2`（70-279 行）是一张 SDL→PS/2 set2 扫描码大表，`poll_fb_window_event`（296-316 行）在 `SDL_KEYDOWN/UP` 时经 `convert_and_enqueue_scancode` 调 `enqueue_key` 入队并触发 `INT_PS2_RX`。这属于「帧缓冲窗口」模块的输入侧。

#### 4.2.4 代码实践

1. **目标**：亲眼看到帧缓冲映射，并定位地址流转。
2. **步骤**：
   - 构建 colorbars（`DISPLAY_WIDTH 640 DISPLAY_HEIGHT 480`，见其 [CMakeLists.txt](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/colorbars/CMakeLists.txt)）。构建系统会自动生成 `run_emulator` 脚本，内容等价于 `nyuzi_emulator -f 640x480 colorbars.hex`（见 [cmake/nyuzi.cmake:78-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L78-L92)）。
   - 运行它，应弹出一个 640×480 的 SDL 窗口，画出滚动的彩条。
   - 在 [colorbars/main.cpp:30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/colorbars/main.cpp#L30) 确认帧缓冲默认基址 `0x200000`，在 [35-36 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/colorbars/main.cpp#L35-L36) 确认 `kScreenWidth/Height = 640/480`，与 `init_vga` 返回的 `FB_BASE` 一致。
3. **观察**：窗口画面 = 模拟内存 `0x200000` 起共 \(640 \times 480 \times 4 = 1228800\) 字节的逐像素 ABGR 内容。
4. **预期结果**：能解释「软件写像素到 `0x200000` ⇒ `set_frame_buffer_address` 记下该地址 ⇒ `update_frame_buffer` 用 `get_memory_region_ptr` 取出并贴图」的链路。
5. 是否一定弹窗：依赖宿主有显示环境（无头环境需用 `-d` 转储内存代替）；运行表现待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `-r` 设得很大（如 `-r 5000000`），画面会有什么变化？为什么？
**答**：主循环每跑 500 万条指令才刷新一帧，画面明显卡顿/跳变。因为 `execute_instructions(proc, screen_refresh_rate)` 的第二个参数就是两次刷新之间执行的指令数。

**练习 2**：软件没写 `REG_VGA_ENABLE=1` 之前，窗口会显示什么？
**答**：什么都不显示。`update_frame_buffer` 开头的 `if (!fb_enabled) return;` 会直接返回，不读内存也不贴图。

---

### 4.3 虚拟块设备：用一个宿主文件假装成 SD 卡

#### 4.3.1 概念说明

很多应用（如 sceneview 渲染三维场景）需要从「磁盘」读资源文件。FPGA 上这是一张插在 SPI 总线上的真实 SD 卡；模拟器里，则用宿主机上的**一个普通文件**充当这张卡。

实现思路是「**字节级 SPI 从机状态机**」：软件像操作真卡一样，拉低片选、逐字节发命令、收响应；模拟器的 `transfer_sdmmc_byte` 每被调用一次就「吃进一个字节、吐回一个字节」，并在内部推进一个状态机，把命令翻译成对宿主文件的 `lseek`/`read`/`write`。对软件完全透明——同一份 SD 驱动代码两边都能用。

#### 4.3.2 核心流程

SD 协议在 SPI 模式下的简化模型：

```
主机(软件)                  从机(模拟器 sdmmc.c)
─────────────────────────   ─────────────────────────────────
拉低 CS (REG_SD_CONTROL=0)
发命令帧 6 字节:             STATE_RECEIVE_COMMAND: 收齐 6 字节
  [cmd | addr(4) | crc]      -> process_command(): 按命令分支
                               CMD17 READ_SINGLE_BLOCK:
                                  lseek+read 宿主文件到 block_buffer
                               CMD24 WRITE_SINGLE_BLOCK:
                                  准备接收数据
收 R1 响应字节               STATE_SEND_R1 / READ_CMD_RESPONSE
收数据令牌 0xfe              STATE_READ_DATA_TOKEN
逐字节读出块数据(+2字节CRC)   STATE_READ_TRANSFER: 逐字节回放 block_buffer
```

关键常量：`INIT_CLOCKS=80`（上电初始化时钟数）、`SD_COMMAND_LENGTH=6`、`DATA_TOKEN=0xfe`、默认 `block_length=512`。状态机用 `enum sd_state`（14 个状态）描述从上电等待到读/写完成的全部阶段。

#### 4.3.3 源码精读

**命令与状态枚举。** sdmmc.c 顶部定义了命令码与状态机：

[tools/emulator/sdmmc.c:38-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c#L38-L65) `enum sd_command`（CMD0/1/8/16/17/24/41/55 等）与 `enum sd_state`（STATE_INIT_WAIT…STATE_WRITE_DATA_RESPONSE）。

**打开宿主文件作块设备。** `open_sdmmc_device` 由 main.c 的 `-b` 选项调用：

[tools/emulator/sdmmc.c:83-106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c#L83-L106) `stat`+`open(O_RDWR)` 打开宿主文件，置 `block_length=512` 并 `malloc` 一块 `block_buffer` 作为单块读写缓冲。

**命令派发。** 收齐 6 字节命令后 `process_command` 分支处理：

[tools/emulator/sdmmc.c:177-213](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c#L177-L213) `CMD_READ_SINGLE_BLOCK`：按 `地址 × block_length` 算出文件偏移，`lseek`+`read` 把整块读进 `block_buffer`，进入读响应态并加一个随机延迟（`state_delay = next_random() & 0xf`）；`CMD_WRITE_SINGLE_BLOCK`：记下偏移、进入写响应态。（注意 `read_little_endian` 把命令里的 4 字节地址拼成大端式的 32 位数，对应 SD 命令地址字段格式。）

**字节级状态机。** `transfer_sdmmc_byte` 是真正的「SPI 从机」：

[tools/emulator/sdmmc.c:227-385](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c#L227-L385) 按 `current_state` 大 switch：`STATE_INIT_WAIT` 数够 80 个初始化时钟；`STATE_IDLE/RECEIVE_COMMAND` 识别命令帧头（`(value & 0xc0)==0x40`）并凑齐 6 字节；`STATE_SEND_R1/R3/R7` 回各种响应；`STATE_READ_TRANSFER` 逐字节回放 `block_buffer`（末尾补 2 字节被忽略的 CRC）；`STATE_WRITE_TRANSFER` 逐字节填 `block_buffer`，到 `STATE_WRITE_DATA_RESPONSE` 时 `lseek`+`write` 落回宿主文件（365-381 行）。

**与分发层的接线。** device.c 把 SD 寄存器接到上面两个函数：

[tools/emulator/device.c:51-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L51-L57) `REG_SD_WRITE_DATA`→`last_sdmmc_response = transfer_sdmmc_byte(value)`（发一字节、记住回值）；`REG_SD_CONTROL`→`set_sdmmc_cs(value & 1)`（片选）。

[tools/emulator/device.c:118-122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L118-L122) `REG_SD_READ_DATA` 回 `last_sdmmc_response`（取出上次的回值）；`REG_SD_STATUS` 恒回 1（就绪）。可见软件必须**先写一字节、再读一字节**，符合 SPI 半双工时序。

**挂载与卸载。** main.c 的 `-b` 选项打开设备，结束时关闭：

[tools/emulator/main.c:241-246](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L241-L246) `case 'b'` 调 `open_sdmmc_device(optarg)` 并记 `block_device_open=true`；程序末尾 [main.c:441-442](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L441-L442) 调 `close_sdmmc_device`。

#### 4.3.4 代码实践

1. **目标**：用 `-b` 挂载虚拟块设备并验证 SD 读路径。
2. **步骤**：
   - 构建 sceneview。它的 [CMakeLists.txt](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/CMakeLists.txt) 用 `FS_IMAGE_FILES` 把三维场景资源打包成 `fsimage.bin`，生成的 `run_emulator` 会自动带上 `-b fsimage.bin`（见 [cmake/nyuzi.cmake:82-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L82-L84)、[91-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L91-L92)）。
   - 运行 `run_emulator`，sceneview 启动后会通过 SD 驱动发 `CMD17` 读场景文件；模拟器经 `transfer_sdmmc_byte` → `process_command` → `lseek/read(fsimage.bin)` 把数据回放给软件，最终渲染出三维场景。
   - 也可手动验证：`bin/nyuzi_emulator -b somefile.bin -f 640x480 <app>.hex`，在 [sdmmc.c:191](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/sdmmc.c#L191) 的 `read(block_fd, block_buffer, block_length)` 处下断点/加 `printf`，观察读块偏移与长度。
3. **观察**：每次软件读一个 512 字节块，对应宿主文件 `fsimage.bin` 的一次 `lseek+read`。
4. **预期结果**：能解释「软件 `CMD17` ⇒ 宿主文件 `read` ⇒ 字节回放」的映射，并说明软件用的是与真机相同的 SD 驱动。
5. 资源打包细节与实际渲染效果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：软件读 `REG_SD_READ_DATA` 得到的字节，是它**刚刚写**到 `REG_SD_WRITE_DATA` 的那一个字节对应的响应吗？为什么？
**答**：是。SPI 是半双工：软件写 `REG_SD_WRITE_DATA` 触发 `transfer_sdmmc_byte(value)`，返回值存入 `last_sdmmc_response`；软件随后读 `REG_SD_READ_DATA` 取出的正是这个回值。一字节进、一字节出。

**练习 2**：为什么 `CMD_READ_SINGLE_BLOCK` 之后要有一个 `state_delay = next_random() & 0xf` 的随机等待？
**答**：真实 SD 卡在收到读命令到给出数据之间有不确定的忙时（卡在忙状态）。加随机延迟更贴近真实时序，能暴露软件里「不等就绪就读」一类的时序敏感缺陷。

---

### 4.4 UART：printf 如何到达宿主终端

#### 4.4.1 概念说明

UART（通用异步收发）是最朴素的串口：一条线发、一条线收，逐字节传输。Nyuzi 把它映射成两个寄存器——一个「写它就把字节发出去」（TX），一个「读它就拿到对方发来的字节」（RX），再加一个状态寄存器表示「收缓冲里有没有数据」。

在模拟器里，**TX 直接接到宿主的 stdout**（所以程序的 `printf` 就出现在你的终端），**RX 接到宿主的 stdin**（你在终端敲字，就当成串口输入喂给程序）。这条「TX→stdout」链路正是 u1-l4 里 `printf` 输出的落地之处，本讲把它和设备分发串起来。

#### 4.4.2 核心流程

```
输出方向 (printf -> 终端):
   软件 printf -> vfprintf -> _write_uart
   -> REGISTERS[REG_UART_TX] = ch          # 字节地址 0xffff0048
   -> write_device_register(0xffff0048, ch) # device.c
   -> putc(ch & 0xff, stdout); fflush       # 宿主终端

输入方向 (终端 -> 软件):
   主循环 poll_inputs(): 若 stdin 可读
   -> read(STDIN) -> enqueue_serial_char(每个字节)
   -> 写入 serial_read_buf + raise INT_UART_RX
   软件: 读 REG_SERIAL_STATUS 的 bit1 判就绪
   -> 读 REG_SERIAL_INPUT 取字节; 缓冲空则 clear INT_UART_RX
```

#### 4.4.3 源码精读

**TX：写字节即输出。** device.c：

[tools/emulator/device.c:46-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L46-L49) `REG_SERIAL_OUTPUT` → `putc(value & 0xff, stdout)` 并 `fflush`。这就是一切 `printf` 的终点。

**RX：状态与数据寄存器 + 缓冲。** device.c 的读分支：

[tools/emulator/device.c:79-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L79-L95) `REG_SERIAL_STATUS` 回 `1 | (有数据 ? 2 : 0)`——bit0 恒就绪、bit1 表示收缓冲非空；`REG_SERIAL_INPUT` 从环形缓冲 `serial_read_buf` 取一字节，取空后 `clear_interrupt(proc, INT_UART_RX)`。

**入队与中断。** `enqueue_serial_char` 由主循环调用：

[tools/emulator/device.c:141-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L141-L152) 把字节写入环形缓冲，满了丢最旧（注释承认应置 overrun 标志但暂未实现），并 `raise_interrupt(proc, INT_UART_RX)`。

**主循环轮询 stdin。** main.c：

[tools/emulator/main.c:100-111](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L100-L111) 若 `can_read_file_descriptor(STDIN_FILENO)`，则 `read` 最多 64 字节，逐字节 `enqueue_serial_char`——把你在终端敲的字变成串口输入。同函数上方（83-98 行）还轮询 `-i` 命名管道，把外部进程发来的字节当作外部中断 `raise_interrupt(1 << id)`。

> 顺带：键盘走的是另一条对称的链路（`enqueue_key` → `key_buf` → `REG_KEYBOARD_STATUS/READ`，中断 `INT_PS2_RX`），其输入来自 4.2 里 SDL 窗口的按键事件。device.c 的 `enqueue_key` 在 [129-139 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L129-L139)。

#### 4.4.4 代码实践

1. **目标**：用 `-v` 跟踪验证 TX 链路。
2. **步骤**：
   - 构建 hello_world（见 u1-l4），运行 `bin/nyuzi_emulator -v hello_world.hex`。
   - 在终端观察：一方面 `printf` 的文本直接出现在 stdout（经 `REG_SERIAL_OUTPUT`→`putc`），另一方面 `-v` 的寄存器转移踪迹里能看到对 `0xffff0048` 的 store。
3. **观察**：每次软件写 `0xffff0048`，紧跟着宿主终端多出一个字符。
4. **预期结果**：能写出「`printf("Hi")` → `_write_uart` → `store_32(0xffff0048,'H')` → `write_device_register` → `putc`」的完整链。
5. 运行结果：待本地验证（`-v` 踪迹格式可对照 [tools/emulator/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md) 的 Tracing 一节）。

#### 4.4.5 小练习与答案

**练习 1**：`REG_SERIAL_STATUS` 的 bit0 为什么恒为 1？
**答**：它表示「TX 就绪/可写」。模拟器的 TX 直接接到宿主 stdout，永远可立即接收，故恒为 1；软件据此可以无阻塞地连续发送。

**练习 2**：如果软件一直不读 `REG_SERIAL_INPUT`，不停有字符到来，会发生什么？
**答**：`serial_read_buf` 是 64 字节环形缓冲，满了之后 `enqueue_serial_char` 会覆盖最旧字节（丢字符）。注释指出严格说应置 overrun 标志，但当前实现没有。

---

## 5. 综合实践

把四个模块串起来，做一次「端到端外设追踪」。

**任务**：构建并运行 **sceneview**，然后为下面这条「软件操作 → 模拟器副作用」对照表填出每一步经过的关键函数与文件行号。

| 软件做的事 | 写/读的寄存器（字节地址） | 模拟器里的落点（函数） | 宿主侧效果 |
|------------|--------------------------|------------------------|------------|
| `printf` 输出帧率 | `REG_UART_TX` (`0xffff0048`) | `write_device_register`→`putc` | 终端出现文字 |
| 初始化显示 | `REG_VGA_BASE/ENABLE` (`0x188/0x180`) | `set_frame_buffer_address`/`enable_frame_buffer` | SDL 窗口可刷新 |
| 读场景文件 | `REG_SD_*` (`0xc0~0xcc`) | `transfer_sdmmc_byte`→`lseek/read` | 读 `fsimage.bin` |
| （可选）按键交互 | `REG_KEYBOARD_*` (`0x80/0x84`) | `enqueue_key`→`key_buf` | SDL 按键入队 |

**操作步骤**：

1. 在仓库根执行 `cmake . && make sceneview`（或整体 `make`）。
2. 进入 sceneview 构建目录，运行自动生成的 `run_emulator`（等价于 `nyuzi_emulator -f 640x480 -b fsimage.bin -c 0x8000000 sceneview.hex`，见 [cmake/nyuzi.cmake:82-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L82-L92)）。
3. 对照上表，在源码里逐一找到对应函数，核实行号。
4. 进阶：在 [device.c:47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L47) 的 `putc` 前临时加一句 `fprintf(stderr, "UART %02x\n", value & 0xff);`（**仅本地调试，勿提交**），重新编译运行，验证每写一个字符就有一行 stderr 输出。

**预期结果**：能完整复述「一次外设访问 = `translate_address` → 设备区判定 → `read/write_device_register` 的 switch → 具体外设的宿主副作用」这条统一链路，并理解模拟器用宿主资源（stdout、SDL、文件）替代真实外设硬件的设计取舍。

> 说明：本实践依赖宿主图形环境与已构建的工具链；若在无头 CI 中运行，图形与交互部分可用 `-d` 内存转储、`-v` 跟踪替代观察，运行表现待本地验证。

## 6. 本讲小结

- **统一分发层**：所有 `>= 0xffff0000` 的物理地址都不走内存数组，而经 processor.c 的设备区判定，交给 device.c 的 `read/write_device_register` 用 switch 分发；设备访问只接受 32 位字（`MEM_LONG`），块/scatter 访问设备地址会崩溃。
- **同一套硬件、两种命名**：软件侧 `registers.h` 的 `REG_UART_TX` 等与模拟器侧 `device.h` 的 `REG_SERIAL_OUTPUT` 等共享同一字节地址，保证软件在模拟器与 FPGA 上行为一致。
- **帧缓冲是「贴图」而非「时序」**：模拟器忽略 VGA 微码/时序寄存器，只用 `REG_VGA_BASE`/`REG_VGA_ENABLE` 拿到帧缓冲地址与开关，周期性把那块模拟内存用 SDL 贴到宿主窗口。
- **虚拟 SD 卡是字节级 SPI 状态机**：`transfer_sdmmc_byte` 逐字节驱动 14 态状态机，把 SD 命令翻译成对宿主文件的 `lseek`/`read`/`write`，对软件完全透明。
- **UART 即 stdio**：TX→`putc(stdout)` 让 `printf` 落地终端；RX←stdin 轮询 + 环形缓冲 + `INT_UART_RX` 中断实现串口输入。
- **外设事件经 `raise_interrupt`/`clear_interrupt` 变成中断位**（`INT_UART_RX`/`INT_PS2_RX`/`INT_VGA_FRAME`），与 u7-l2 的中断机制衔接。

## 7. 下一步学习建议

- **验证如何利用这些外设**：读 [tests/cosimulation/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md)，进入下一讲 **u8-l3 协同仿真验证机制**，看模拟器如何作为「功能金标准」与硬件逐指令比对。
- **回到硬件侧对照**：本讲的外设在 FPGA 上对应真实 RTL，可读 [hardware/fpga/common/uart.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv)、`vga_controller.sv`、`sdram_controller.sv`（u14 单元），体会「软件级仿真」与「周期级硬件」的取舍。
- **调试实践**：结合 u8-l1 的 `-v` 与本讲的设备链路，再用 `-m gdb`（u11-l3）做源码级单步，观察一次 `printf` 在寄存器层面的真实发生过程。
- **扩展练习**：仿照 device.c 现有 case，尝试为某个「未实现」寄存器（如 LED）加一个 case，把写操作转发到宿主的一种可见副作用（如 `fprintf(stderr,...)`），从而理解增删外设的改动量有多小。
