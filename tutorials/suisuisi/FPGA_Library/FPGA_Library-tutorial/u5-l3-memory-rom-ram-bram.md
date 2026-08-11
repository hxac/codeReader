# 存储器：ROM/RAM 与 BRAM 推断

## 1. 本讲目标

本讲聚焦 `ThreePart/projf-explore/lib/memory/` 下的三个最小存储模块：`rom_async`、`rom_sync`、`bram_sdp`。学完后你应当能够：

- 区分**异步 ROM**（组合读，不占时钟）、**同步 ROM**（打一拍读）、**双口 BRAM**（独立读写口）三者的接口与时序差异；
- 看懂这三个模块的源码，理解 `WIDTH/DEPTH/INIT_F/ADDRW` 四个共享参数的含义；
- 掌握一条核心工程经验：**「同步读」是让综合工具把数组推断为 Block RAM 的关键**，而「异步读」只能落到 LUT（分布式 RAM）；
- 自己写一个 testbench，对 `bram_sdp` 先写后读、观察一拍读延迟，并能在综合报告中确认它被推断为 Block RAM。

承接 [u5-l1](u5-l1-verilog-library-overview.md)（projf 库总览与 SystemVerilog 风格）和 [u5-l2](u5-l2-clock-domain-crossing.md)（时钟与跨时钟域），本讲进入 projf 七大分区中的 **memory 分区**。

## 2. 前置知识

### 2.1 FPGA 里「存储」的三种物理资源

在通用处理器里，内存就是内存。但在 FPGA 里，一段「数组」最终会被综合工具映射到三种完全不同的物理资源之一，它们的容量、速度、数量差别极大：

| 物理资源 | 典型来源 | 特点 | 数量（相对） |
| --- | --- | --- | --- |
| **触发器（FF）** | `reg` 直接存 | 最快、最灵活，但一个比特就占一个触发器 | 少 |
| **LUT / 分布式 RAM（LUTRAM）** | 小数组、异步读数组 | 速度快、可异步（组合）读 | 中 |
| **Block RAM（BRAM）** | 大数组、同步读数组 | 容量大（7 系单块 36Kb）、必须时钟读 | 较多但有限 |

打个比方：触发器像你桌上的便签（随手记、容量小），LUTRAM 像抽屉（稍多、拿取快），BRAM 像档案柜（容量大，但要走流程——先报地址，下一拍才拿到）。本讲的核心，就是**怎么写代码让一段数组「被识别为档案柜（BRAM）」而不是「被塞进抽屉（LUTRAM）」**。

### 2.2 ROM 与 RAM 的区别

- **ROM（Read-Only Memory）**：内容在「上电初始化」时写死，之后只读。本仓库里 ROM 的内容由 `$readmemh` 从一个 `.mem` 文件加载。
- **RAM（Random Access Memory）**：运行时可读可写。

需要强调：这里的 ROM/RAM 是**逻辑用途**上的区分，和上面那张物理资源表是两个维度。一段 ROM 既可能被综合成 BRAM，也可能被综合成 LUTRAM，取决于它怎么读（同步还是异步）。

### 2.3 三个 SystemVerilog 关键字回顾

projf 库只用一个很小的 SystemVerilog 子集（见 u5-l1），本讲会反复用到：

- `logic`：统一的硬件数据类型，替代 `wire`/`reg` 的二选一烦恼；
- `always_ff @(posedge clk)`：**时序逻辑**，描述「时钟沿到来才更新」的行为，综合成触发器；
- `always_comb`：**组合逻辑**，描述「输入一变输出立刻变」的行为，综合成纯逻辑门/LUT。

记住一句话：**`always_ff` 带来一拍延迟和触发器；`always_comb` 不带延迟、纯组合。** 这句话是理解本讲三个模块时序差异的钥匙。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `lib/memory/rom_async.sv` | 29 | 异步 ROM：组合读，无时钟，通常推断为 LUTRAM |
| `lib/memory/rom_sync.sv` | 32 | 同步 ROM：打一拍读，可被推断为 BRAM |
| `lib/memory/bram_sdp.sv` | 42 | 简单双口 Block RAM：一个写口 + 一个读口，支持双时钟 |
| `lib/memory/README.md` | 36 | memory 分区索引，列出模块清单与共享参数 |

辅助参考（用于综合实践与真实用法对照）：

| 文件 | 作用 |
| --- | --- |
| `lib/display/clut_simple.sv` | 用 `bram_sdp` 实现的简单颜色查找表（CLUT） |
| `demos/life-on-screen/lib/display/framebuffer_bram.sv` | 真实工程中用 `bram_sdp` 当帧缓冲、用 `rom_async` 当调色板的范例 |
| `demos/ad-astra/xc7/sprite_tb.sv` | 用 `rom_sync` 当字模 ROM 的 testbench 范例 |
| `demos/ad-astra/res/font_unscii_8x8_latin_uc.mem` | `$readmemh` 加载的 `.mem` 文件格式实例 |

> 永久链接的固定前缀为：
> `https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/`

## 4. 核心概念与源码讲解

### 4.1 三个模块共享的概念基础

#### 4.1.1 概念说明

`rom_async`、`rom_sync`、`bram_sdp` 三个模块长得很像，因为它们共享同一套参数化思想。先在 README 里一次性看清这组共享参数：

[ThreePart/projf-explore/lib/memory/README.md:26-31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/README.md#L26-L31)
> README 列出四个共享参数：`WIDTH`（数据位宽）、`DEPTH`（元素个数）、`INIT_F`（初始化文件）、`ADDRW`（地址位宽，默认由 `$clog2(DEPTH)` 算出）。

这套参数背后是一个简单的存储模型：一段存储就是「`DEPTH` 个、每个 `WIDTH` 位」的数组。地址位宽由深度决定：

\[
\text{ADDRW} = \lceil \log_2 \text{DEPTH} \rceil
\]

总存储位数为：

\[
\text{总比特} = \text{WIDTH} \times \text{DEPTH}
\]

例如 `WIDTH=8, DEPTH=256`，则 `ADDRW=8`，总容量 \(8 \times 256 = 2048\) 比特（2Kb）。projf 用 `localparam ADDRW=$clog2(DEPTH)` 把地址位宽自动算好，调用者通常不需要手填。

#### 4.1.2 核心流程：用 `$readmemh` 初始化

三个模块的「初始化」代码几乎逐字相同：

```systemverilog
logic [WIDTH-1:0] memory [DEPTH];   // 存储数组

initial begin
    if (INIT_F != 0) begin
        $display("...init file '%s'...", INIT_F);
        $readmemh(INIT_F, memory);   // 从文件加载内容
    end
end
```

- `logic [WIDTH-1:0] memory [DEPTH]` 声明一个 `DEPTH` 深、每元素 `WIDTH` 位的**非组合数组**（unpacked array），这就是物理存储。
- `$readmemh(file, array)` 是 Verilog 系统任务：把 `file` 里的十六进制数依次填进 `array`，常用于给 ROM 装入内容（上电即生效）。配套的 `$readmemb` 读二进制。注意 `h` = hex、`b` = bin。
- `if (INIT_F != 0)` 是一道守卫：参数默认值是空字符串 `""`，此时不调用 `$readmemh`（否则会用空文件名报错）。把字符串参数与整数 `0` 比较是 projf 的惯用写法——空字符串视作 0，非空字符串视作非 0。

`.mem` 文件就是纯文本十六进制，每行一个或多个字节，`//` 开头是注释。看一个真实的字模文件片段：

[ThreePart/projf-explore/demos/ad-astra/res/font_unscii_8x8_latin_uc.mem:12-13](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/res/font_unscii_8x8_latin_uc.mem#L12-L13)
> 每个字符占 8 行，每行 1 个字节（8 位 = 8 个像素）。例如空格 `U+0020` 是 8 行全 `00`，`U+0021`（`!`）是 `18 18 18 18 18 00 18 00`。这正是 `$readmemh` 要加载的内容。

#### 4.1.3 三者的真正区别在哪里

把共享部分剥掉，三个模块只剩**一行核心代码**不同：

| 模块 | 核心那一行 | 含义 |
| --- | --- | --- |
| `rom_async` | `always_comb data = memory[addr];` | 组合读：地址一变，数据立刻出 |
| `rom_sync` | `always_ff @(posedge clk) data <= memory[addr];` | 同步读：地址给出后，**下一拍**数据才出 |
| `bram_sdp` | 写口 `always_ff` + 读口 `always_ff` | 同步写、同步读，两个独立端口 |

下面三节分别精读。**注意一个反复出现的主题**：`always_ff`（同步读）让综合工具有机会推断 BRAM；`always_comb`（异步读）几乎一定落到 LUTRAM。

---

### 4.2 异步 ROM：rom_async

#### 4.2.1 概念说明

`rom_async` 是最朴素的查表：给我一个地址，我立刻（组合地）吐出对应数据，**不经过时钟**。它适合「数据量小、需要当拍就拿到结果」的场景，比如颜色查找表（CLUT）——给一个 4 位颜色索引，当拍就要查出 12 位 RGB。

代价是：因为读是组合的（`always_comb`），综合工具**无法**把它映射成 Block RAM（BRAM 的读端口永远是时钟同步的）。它会被映射成 LUT（分布式 RAM / 查找表逻辑）。所以 `rom_async` 适合小表，大表用它会很烧 LUT。

#### 4.2.2 核心流程

```text
addr ──► memory[addr] ──(组合逻辑)──► data
          ▲
   INIT_F 经 $readmemh 在 initial 块加载
```

- 上电：`initial` 块用 `$readmemh` 把 `INIT_F` 内容装进 `memory`；
- 运行：`addr` 一变化，`always_comb` 立刻重算 `data = memory[addr]`，无任何时钟、无任何延迟。

#### 4.2.3 源码精读

[ThreePart/projf-explore/lib/memory/rom_async.sv:8-16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_async.sv#L8-L16)
> 模块端口：注意它**没有 `clk`**，只有 `addr` 输入和 `data` 输出。没有时钟正是「异步」的字面含义。

[ThreePart/projf-explore/lib/memory/rom_async.sv:18-25](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_async.sv#L18-L25)
> 声明 `memory` 数组并用 `$readmemh` 初始化，与 4.1.2 讲的共享写法一致。

[ThreePart/projf-explore/lib/memory/rom_async.sv:27](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_async.sv#L27)
> 核心一行：`always_comb data = memory[addr];`。组合读——这就是它和 `rom_sync` 的唯一区别，也是它无法被推断为 BRAM 的根本原因。

**真实用法**：帧缓冲 demo 的调色板就是 `rom_async`：

[ThreePart/projf-explore/demos/life-on-screen/lib/display/framebuffer_bram.sv:169-176](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/life-on-screen/lib/display/framebuffer_bram.sv#L169-L176)
> 例化 `rom_async` 当 CLUT：`WIDTH=3*CHANW`（RGB 拼一起）、`DEPTH=2**CIDXW`（16 或 256 色）、`INIT_F=F_PALETTE`（调色板文件）。颜色索引 `fb_cidx_read_p1` 当地址，当拍查出 `clut_colr`。注意上一行 `fb_cidx_read_p1` 是用 `always_ff` 打过一拍的——作者特意在 BRAM 读出后插一拍寄存器再喂给异步 ROM，是为了改善时序（注释 `improve timing with register between BRAM and async ROM`）。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 `rom_async` 为何适合当 CLUT。
2. **步骤**：打开 `framebuffer_bram.sv` 的 L163–L179，跟踪这条链路：`bram_sdp` 读出颜色索引 → 打一拍 → 喂给 `rom_async` 查色 → 拆成 RGB 写进 linebuffer。
3. **观察**：注意 L163 的注释 `improve timing with register between BRAM and async ROM`，说明在 BRAM 与异步 ROM 之间特意插了一级寄存器。
4. **预期结果**：能说出「异步 ROM 输出是组合的，若直接接在大组合逻辑后面会拖慢时序，所以先用寄存器隔开」。
5. 待本地验证（无开发板也可纯阅读完成）。

#### 4.2.5 小练习与答案

**练习 1**：把 `rom_async` 的 `WIDTH=8, DEPTH=256` 改成 `DEPTH=4096`，地址位宽变成多少？它最可能被综合成什么资源？

> **答案**：`ADDRW = ⌈log₂ 4096⌉ = 12` 位。由于是异步（组合）读，会被综合成 LUTRAM/查找表逻辑，4KB 的表会消耗大量 LUT，并不划算。

**练习 2**：`rom_async` 的 `if (INIT_F != 0)` 里，为什么用 `!= 0` 而不是 `!= ""`？

> **答案**：这是 projf 的惯用写法。Verilog 里把字符串与整数 0 比较：空字符串视作 0、非空字符串视作非 0，因此 `!= 0` 等价于「参数非空」。两种写法在多数仿真器里都可行，projf 统一选了前者。

---

### 4.3 同步 ROM：rom_sync

#### 4.3.1 概念说明

`rom_sync` 和 `rom_async` 几乎一模一样，唯一区别是读出过程**多了一个时钟沿**：你先在 `addr` 上给出地址，下一个时钟上升沿之后，`data` 才更新成 `memory[addr]`。这一拍延迟换来的是——**综合工具有机会把它推断成 Block RAM**。因为真实 BRAM 的读端口本来就是「时钟沿采样地址、下一拍出数据」，`rom_sync` 的行为与之完全吻合。

所以选择口诀是：**当拍就要结果 → `rom_async`（烧 LUT）；能等一拍、表又大 → `rom_sync`（省 BRAM）**。

#### 4.3.2 核心流程

```text
        ┌─────────── posedge clk ───────────┐
addr ──► memory[addr] ──(寄存器输出)──► data   （延迟 1 拍）
```

- `addr` 在某拍出现；
- 下一个 `posedge clk`，`always_ff` 执行 `data <= memory[addr]`，把读出值锁进 `data`；
- 因此 `data` 相对 `addr` 有 1 拍延迟（这正是 BRAM 的「输出寄存器」行为）。

#### 4.3.3 源码精读

[ThreePart/projf-explore/lib/memory/rom_sync.sv:8-17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_sync.sv#L8-L17)
> 端口：与 `rom_async` 相比**多了一个 `clk`**。有时钟即「同步」。

[ThreePart/projf-explore/lib/memory/rom_sync.sv:19-26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_sync.sv#L19-L26)
> 数组声明与 `$readmemh` 初始化，与异步版完全相同。

[ThreePart/projf-explore/lib/memory/rom_sync.sv:28-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_sync.sv#L28-L30)
> 核心三行：`always_ff @(posedge clk) data <= memory[addr];`。与 `rom_async` 那行 `always_comb` 的差别，就决定了它能否进 BRAM。`<=` 是非阻塞赋值，描述「时钟沿统一更新」。

**真实用法**：ad-astra demo 的字模 ROM 就是 `rom_sync`：

[ThreePart/projf-explore/demos/ad-astra/xc7/sprite_tb.sv:48-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/xc7/sprite_tb.sv#L48-L56)
> 例化 `rom_sync` 当字模 ROM：`INIT_F` 指向 `font_unscii_8x8_latin_uc.mem`（即 4.1.2 看到的那个 `.mem` 文件）、`WIDTH=8`、`DEPTH=64*8=512`。字模表较大（512 字节），用同步读可推断为 BRAM，比异步读省 LUT。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：体会 `rom_sync` 的一拍读延迟在真实工程里如何被消化。
2. **步骤**：阅读 `sprite_tb.sv` 的 L71–L78。精灵模块 `spr0_gfx_addr` 在 `spr0_dma` 期间给出字模地址；由于 `rom_sync` 下一拍才出数据，工程里用「提前两拍请求 DMA」来对齐时序（`spr0_dma` 在 `sx>=25`、`spr1_dma` 在 `sx>=27`）。
3. **观察**：理解「同步读的一拍延迟，需要调用方在时序上提前预约」。
4. **预期结果**：能解释为什么 DMA 窗口要开在真正需要像素之前。
5. 待本地验证（纯阅读即可完成）。

#### 4.3.5 小练习与答案

**练习 1**：同样是 `WIDTH=8, DEPTH=512` 的字模表，用 `rom_async` 和 `rom_sync` 各实现一次，哪个更可能进 BRAM？为什么？

> **答案**：`rom_sync`。因为 BRAM 的读端口本身是时钟同步的（沿采样地址、下一拍出数据），`rom_sync` 的 `always_ff` 行为与之匹配；而 `rom_async` 的组合读 BRAM 做不到，只能落 LUTRAM。

**练习 2**：`rom_sync` 的输出 `data` 相对 `addr` 延迟几拍？如果你需要当拍就拿到结果，该改用哪个模块？

> **答案**：延迟 1 拍。若必须当拍拿到，改用 `rom_async`（代价是改落 LUTRAM）。

---

### 4.4 简单双口 Block RAM：bram_sdp

#### 4.4.1 概念说明

`bram_sdp` 是三者中功能最完整的：它是一块**可读可写的存储**（RAM），而且有**两个独立端口**——一个专门写（A 口）、一个专门读（B 口），两个端口甚至可以各用各的时钟。这种「一写口 + 一读口」的结构叫 **Simple Dual-Port（简单双口）**，区别于：

- **单口（Single-Port）**：只有一个口，读写分时共享（本仓库 `ice40/spram.sv` 即单口）；
- **真双口（True Dual-Port）**：两个口都能读能写。

简单双口特别适合「一边生产数据、一边消费数据」的场景，最典型的就是**帧缓冲**：绘制逻辑用写口把像素写进 BRAM，显示逻辑用读口按扫描顺序把像素读出来送屏幕。两个端口用不同时钟时，还能天然做跨时钟域（见 u5-l2）。

#### 4.4.2 核心流程

```text
写口 A：clk_write 上升沿，若 we=1 则 memory[addr_write] <= data_in   （同步写）
读口 B：clk_read  上升沿，data_out <= memory[addr_read]              （同步读，1 拍延迟）
```

两个端口互不干扰：A 在写某个地址时，B 可以同时在读另一个地址。读写都各自对齐到自己的时钟沿。

#### 4.4.3 源码精读

[ThreePart/projf-explore/lib/memory/bram_sdp.sv:8-21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/bram_sdp.sv#L8-L21)
> 端口：两个独立时钟 `clk_write`/`clk_read`；写口有 `we`（写使能）、`addr_write`、`data_in`；读口有 `addr_read`、`data_out`。`ADDRW` 用 `localparam ... = $clog2(DEPTH)` 自动算出。

[ThreePart/projf-explore/lib/memory/bram_sdp.sv:23-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/bram_sdp.sv#L23-L30)
> 数组声明与 `$readmemh` 初始化。注意 `INIT_F` 对 RAM 同样适用——可以在上电时给 RAM 预置内容（比如把一幅默认图像装进帧缓冲）。若 `INIT_F=""`（默认），RAM 初始内容未定义，需由运行时写入。

[ThreePart/projf-explore/lib/memory/bram_sdp.sv:32-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/bram_sdp.sv#L32-L35)
> 写口：`always_ff @(posedge clk_write) if (we) memory[addr_write] <= data_in;`。典型的**同步写**——只在 `we` 有效时的时钟沿才写入。

[ThreePart/projf-explore/lib/memory/bram_sdp.sv:37-40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/bram_sdp.sv#L37-L40)
> 读口：`always_ff @(posedge clk_read) data_out <= memory[addr_read];`。**同步读，1 拍延迟**——这正是让它被推断为 Block RAM 的关键写法。

#### 4.4.4 BRAM 推断的关键写法（本讲最重要的一节）

为什么 `bram_sdp`（和 `rom_sync`）能进 BRAM，而 `rom_async` 不能？答案藏在「读」那一句里。综合工具（Vivado/Yosys）会按下面这套模式匹配来推断 BRAM：

1. **同步读（带输出寄存器）**：`always_ff @(posedge clk) data <= mem[addr];` —— 地址在时钟沿被采样，数据下一拍出现在寄存器里。这与真实 BRAM 的读端口行为**完全一致**，工具会推断为 Block RAM（带输出寄存器）。✅ `bram_sdp`、`rom_sync` 都是这种。
2. **异步读（组合）**：`always_comb data = mem[addr];` 或 `assign data = mem[addr];` —— 数据当拍就出，没有时钟。真实 BRAM 做不到这件事，工具只能推断为 LUTRAM（分布式 RAM）。❌ `rom_async` 就是这种。
3. **同步写**：`always_ff @(posedge clk) if (we) mem[addr] <= din;` —— BRAM 写端口的标准行为，配合同步读即得标准 BRAM 模板。

**记忆口诀**：

> 写要 `always_ff` 同步写，读要 `always_ff` 同步读（带寄存器输出）→ 推断 BRAM；
> 一旦读变成 `always_comb`/`assign` → 退化为 LUTRAM。

还有两条工程上的注意点：

- **小表不一定进 BRAM**：即便写法正确，若容量太小（如 `DEPTH=16`），综合工具可能仍用 LUTRAM 更划算——Block RAM 有最小粒度（7 系单块 36Kb），小块用 BRAM 反而浪费。
- **复位要小心**：BRAM 的存储阵列**不应该有复位**。如果你给 `memory` 数组加了复位（`if (rst) memory[...] <= 0`），工具往往就不再推断 BRAM，而改成一大堆触发器——本仓库这三个模块都**没有**给存储阵列复位，正是为了保住 BRAM 推断。

**真实用法 1——颜色查找表（小表 RAM）**：

[ThreePart/projf-explore/lib/display/clut_simple.sv:22-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/clut_simple.sv#L22-L34)
> `clut_simple` 直接把 `bram_sdp` 包了一层当 CLUT：写口用来运行时改调色板，读口用颜色索引查颜色。两个口共用同一对时钟。

**真实用法 2——帧缓冲（大表 RAM，projf 最典型的 BRAM 用法）**：

[ThreePart/projf-explore/demos/life-on-screen/lib/display/framebuffer_bram.sv:79-91](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/life-on-screen/lib/display/framebuffer_bram.sv#L79-L91)
> 例化 `bram_sdp` 当帧缓冲：`DEPTH=WIDTH*HEIGHT`（如 320×180=57600 像素）、`WIDTH=CIDXW`（4 位颜色索引）。绘制逻辑通过 `addr_write/data_in/we` 写像素，显示逻辑通过 `addr_read` 读像素。注意这里 `clk_write` 和 `clk_read` 都接到 `clk_sys`（同频同相），是为了简化；但 `bram_sdp` 本身支持双时钟。

#### 4.4.5 代码实践（源码阅读型）

1. **目标**：理解 `bram_sdp` 双口结构如何支撑「边画边显示」。
2. **步骤**：阅读 `framebuffer_bram.sv` 的 L57–L91。写地址 `fb_addr_write` 由像素坐标 `(x,y)` 算出（`WIDTH*y + x`，分两级流水），读地址 `fb_addr_read` 由扫描计数器递增。
3. **观察**：写口和读口各自有自己的地址生成逻辑，互不阻塞。
4. **预期结果**：能说出「简单双口让绘制与显示可以并行进行，互不卡顿」。

#### 4.4.6 小练习与答案

**练习 1**：如果把 `bram_sdp` 读口改成 `always_comb data_out = memory[addr_read];`，会发生什么？

> **答案**：读变成组合的，综合工具将无法推断为 Block RAM，会退化成 LUTRAM（分布式 RAM），大幅消耗 LUT 资源；且大帧缓冲（几万深度）几乎肯定资源爆炸。这正是「同步读」对 BRAM 推断的必要性。

**练习 2**：`bram_sdp` 的读延迟是几拍？为什么 `framebuffer_bram.sv` 里要算一个 `LAT=3` 的延迟对齐？

> **答案**：`bram_sdp` 读延迟 1 拍（`data_out` 在 `addr_read` 给出后的下一个 `clk_read` 沿才更新）。但帧缓冲整条链路除了 BRAM 本身，还有地址计算、CLUT 寄存器等额外延迟，合计 3 拍，所以工程里用 `lb_en_in_sr` 移位寄存器把「使能」也延迟 3 拍来对齐（见 `framebuffer_bram.sv` L107–L112、L134）。

---

## 5. 综合实践：给 bram_sdp 写一个最小 testbench

本任务把第 4 节的知识串起来：**仿真**验证读写功能与 1 拍读延迟，**综合**验证它确实被推断为 Block RAM。任务对应规格要求的实践目标。

### 5.1 实践目标

- 用一个独立 testbench 例化 `bram_sdp`，先写入若干字节、再读回，确认数据正确；
- 在波形上测量「读延迟」并验证它正好是 1 拍；
- （可选）跑一次综合，在利用率报告中确认 Block RAM 被使用。

### 5.2 示例 testbench

下面是**示例代码（非仓库原有文件）**，把它存为 `tb_bram_sdp.sv`，与 `bram_sdp.sv` 放在同一仿真目录：

```systemverilog
// 示例代码：bram_sdp 的最小 testbench（非仓库原有文件）
`default_nettype none
`timescale 1ns / 1ps

module tb_bram_sdp();
    // 选用小容量，便于人眼跟踪波形
    localparam WIDTH = 8;
    localparam DEPTH = 16;

    logic clk;
    logic we;
    logic [$clog2(DEPTH)-1:0] addr_write, addr_read;
    logic [WIDTH-1:0] data_in;
    logic [WIDTH-1:0] data_out;

    // 100 MHz 时钟：周期 10ns
    initial clk = 0;
    always #5 clk = ~clk;

    // 例化被测模块（DUT）
    bram_sdp #(
        .WIDTH(WIDTH),
        .DEPTH(DEPTH)
    ) dut (
        .clk_write(clk),
        .clk_read(clk),
        .we,
        .addr_write,
        .addr_read,
        .data_in,
        .data_out
    );

    integer i;
    initial begin
        // 监视关键信号，方便观察读延迟
        $monitor("t=%0t  we=%b  aw=%0d  dw=%0h  ar=%0d  dout=%0h",
                 $time, we, addr_write, data_in, addr_read, data_out);

        we = 0; addr_write = 0; data_in = 0; addr_read = 0;
        #2;

        // ---- 阶段 1：顺序写入 0..3 号地址，值 = 地址 + 8'hA0 ----
        for (i = 0; i < 4; i = i + 1) begin
            @(negedge clk);              // 在下降沿改激励，避免与时钟沿竞争
            we = 1;
            addr_write = i;
            data_in = i[7:0] + 8'hA0;    // 0->A0, 1->A1, 2->A2, 3->A3
        end
        @(negedge clk);
        we = 0;

        // ---- 阶段 2：读回 2 号地址，观察读延迟 ----
        @(negedge clk);
        addr_read = 2;                   // 第 N 拍前给出地址
        // 期望：下一个 posedge clk 之后 data_out 才变成 A2（1 拍延迟）
        @(posedge clk);                  // 此拍沿采样地址
        @(posedge clk);                  // 此拍沿 data_out 更新
        if (data_out === 8'hA2)
            $display("[OK]   addr=2 读回 A2，读延迟 = 1 拍");
        else
            $display("[FAIL] addr=2 读回 %0h，期望 A2", data_out);

        #20 $finish;
    end
endmodule
```

### 5.3 操作步骤

1. 准备文件：把上面 testbench 存为 `tb_bram_sdp.sv`，与原 `bram_sdp.sv`（路径 `ThreePart/projf-explore/lib/memory/bram_sdp.sv`）放进同一目录。
2. 选择仿真器（任一即可）：
   - **Icarus Verilog**：`iverilog -g2012 -o sim.vvp tb_bram_sdp.sv bram_sdp.sv && vvp sim.vvp`
   - **Verilator**：`verilator --binary -Wall --timing -sv tb_bram_sdp.sv bram_sdp.sv`（注意 Verilator 对 `$monitor`/initial 时钟块需要 `--timing`）
   - **Vivado 仿真器**：把两个文件加进工程，设 `tb_bram_sdp` 为 top，Run Simulation。
3. 观察终端 `$monitor` 打印与 `$display` 的 `[OK]/[FAIL]` 结论。
4. （可选）用 GTKWave 或 Vivado 波形窗看 `clk / addr_read / data_out` 的时间关系。

### 5.4 需要观察的现象与预期结果

- **写入**：`we=1` 的 4 个下降沿，依次把 A0/A1/A2/A3 写进地址 0/1/2/3。
- **读延迟**：在给出 `addr_read=2` 之后，`data_out` **不是立刻**变成 A2，而是**下一个 `posedge clk` 之后**才变成 A2——这就是 `bram_sdp.sv` 第 38–40 行 `always_ff @(posedge clk_read) data_out <= memory[addr_read]` 带来的 1 拍延迟。
- **结论行**：终端应打印 `[OK] addr=2 读回 A2，读延迟 = 1 拍`。
- 若把 testbench 里读地址改成 3，应读到 A3，同理延迟 1 拍。

> 若你的仿真器对 `logic` 数组初始化或 `$clog2` 有差异，结果以实际为准——**待本地验证**。

### 5.5 （可选）综合验证 BRAM 推断

完成仿真后，把 `bram_sdp` 设成一个小工程的顶层（或直接综合上面 testbench 中的例化），在 Vivado 中：

1. Run Synthesis；
2. 打开 **Synthesized Design → Reports → Report Utilization**；
3. 查看 `Block RAM` 一栏的使用数。

把参数改成更接近真实帧缓冲的容量（如 `WIDTH=4, DEPTH=57600`），预期 `Block RAM` 使用数明显大于 0，证明它被推断为 BRAM。对照实验：把读口临时改成 `always_comb data_out = memory[addr_read];` 再综合一次，`Block RAM` 应归零、`LUT as Memory` 上升——这就直观验证了第 4.4.4 节的「同步读 → BRAM / 异步读 → LUTRAM」规律。注意：对照实验需要你临时改一份本地副本，**不要修改仓库源码**。

## 6. 本讲小结

- 三个模块共享 `WIDTH/DEPTH/INIT_F/ADDRW` 四个参数，`ADDRW` 由 `$clog2(DEPTH)` 自动算出，`INIT_F` 经 `$readmemh` 从 `.mem` 文件加载内容。
- `rom_async`（`always_comb`，组合读，无时钟）适合小表、当拍取数，但只能落到 LUTRAM。
- `rom_sync`（`always_ff`，同步读，1 拍延迟）行为匹配真实 BRAM 读端口，可被推断为 Block RAM。
- `bram_sdp` 是简单双口 RAM（一写口 + 一读口，支持双时钟），是 projf 帧缓冲与 CLUT 的主力存储。
- **核心工程经验**：写要 `always_ff` 同步写、读要 `always_ff` 同步读（带输出寄存器）→ 推断 BRAM；读一旦变组合 → 退化为 LUTRAM；存储阵列不要加复位，否则也保不住 BRAM。
- 真实工程里 `rom_async` 当调色板、`rom_sync` 当字模表、`bram_sdp` 当帧缓冲，三者各司其职。

## 7. 下一步学习建议

- **横向**：去看 `lib/memory/ice40/spram.sv`——它是 iCE40 平台的**单口 RAM**，直接例化 Lattice 硬核原语 `SB_SPRAM256KA`，与 `bram_sdp` 的「可推断 BRAM」写法形成「原语直例 vs. 行为推断」两种风格的对照。
- **纵向（显示）**：本讲的 `bram_sdp` 是 [u6-l1 显示时序](u6-l1-display-timing.md) 与 [u6-l4 帧缓冲与硬件精灵](u6-l4-framebuffer-sprites.md) 的存储基础。帧缓冲、linebuffer 都建立在本讲之上，建议结合 `framebuffer_bram.sv` 一起读。
- **纵向（数学）**：`rom_sync` 也被 [u7-l4 的 sine_table](u7-l4-lfsr-sine-table.md) 用来存正弦样本表，可作为「同步 ROM 存波形」的延伸阅读。
- **延伸阅读**：projf 博客 [Initialize Memory in Verilog](https://projectf.io/posts/initialize-memory-in-verilog/)（深入讲 `$readmemh`/`$readmemb`）与 [SPRAM on iCE40 FPGA](https://projectf.io/posts/spram-ice40-fpga/)（对照 Block RAM 与 SPRAM）。
