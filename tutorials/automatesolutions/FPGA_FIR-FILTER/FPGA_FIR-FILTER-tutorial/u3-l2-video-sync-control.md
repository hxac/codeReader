# 视频时序与 sharp_control 同步延迟

## 1. 本讲目标

本讲承接 u3-l1，把上一讲刻意留下的一个悬念讲透：**顶层为什么给 `sharp_control` 传入 `delay => 6`？** 读完本讲，你应当能够：

- 说清 `vs`、`hs`、`de` 三个视频时序信号各自的含义，以及它们如何把一维像素流“拼”成二维画面；
- 看懂 `sharp_control.vhd` 用一个数组实现的**移位寄存器延迟链**——它如何把信号原样延迟固定拍数，以及 `generic delay` 如何决定延迟长短；
- 用“**行内延迟 + 行级延迟**”两把尺子量化数据通路，推出**为什么同步信号的延迟恰好要取 6 拍**，并解释如果改成 3 或 9 会发生什么。

本讲只聚焦同步与时序对齐；`sharp_slice` 内部抽头的完整数据流是 u3-l3 的主题，`sharp_linemem` 的循环缓冲与 `sharp_arith` 的定点运算分别在 u4-l1、u4-l2 详讲，本讲只在“数延迟拍数”时借用它们的结论。

## 2. 前置知识

本讲承接前置讲义，直接使用其结论，不再重复：

- **u3-l1 顶层 sharp.vhd**：你已经知道顶层在**同一个输入进程**里把 `vs_in/hs_in/de_in` 打成 `vs_0/hs_0/de_0`、把 `r/g/b_in` 转成 `r_0/g_0/b_0`；又在**同一个输出进程**里把 `vs_1/hs_1/de_1` 与 `r_1/g_1/b_1` 一起打成 `*_out`。这两处“同进程打拍”是对齐的锚点。上一讲还给出了像素链路表，指出纯流水线寄存器约 5–6 拍、行存储造成约 3 行延迟——本讲要把这些拍数精确化。
- **u2-l1 / u2-l2 系数设计**：锐化核 \([1,0,-9,48,-9,0,1]/32\)，二维滤波等价于“先垂直、再水平”两次一维卷积，这正是数据通路存在多级延迟的根源。

此外需要一点视频与 VHDL 概念（不熟悉的术语下面会解释）：

- **视频时序（`vs/hs/de`）**：把一串按时间到达的像素重新摆回二维网格所用的“排版信号”，下一节详讲。
- **移位寄存器（shift register）**：一串首尾相接的触发器，每个时钟把内容往后挪一格，整体效果是“把输入延迟若干拍”。
- **generic（类属参数）**：VHDL 实体声明里的编译期常量，例化时可以用 `generic map` 覆盖，用来让同一个模块在不同地方取不同参数。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `FPGA-Design/sharp_control.vhd` | 把 `vs/hs/de` 各延迟 `delay` 拍 | **本讲主角**，逐行精读它的移位寄存器实现 |
| `FPGA-Design/sharp.vhd` | 顶层，例化 `sharp_control` 并喂入/取出同步信号 | 看 `generic map (delay => 6)`、输入/输出进程如何把同步与数据“绑”在一起 |
| `FPGA-Design/sharp_slice.vhd` | 单通道二维滤波（黑盒） | 只数它的“行内延迟拍数”，论证为什么是 6 |
| `FPGA-Design/sharp_arith.vhd` | 7 抽头乘加，寄存器输出 | 只用到“它有一个时钟寄存器输出”这一事实 |
| `FPGA-Design/sharp_linemem.vhd` | 1280 项行存储 | 只用到“它延迟一整行有效像素”这一事实 |

> 提示：`sharp_slice`/`sharp_arith`/`sharp_linemem` 的完整精读分别在 u3-l3、u4-l2、u4-l1。本讲引用它们只为“数拍数”。

## 4. 核心概念与源码讲解

三个最小模块依次是：4.1 视频时序信号 `vs/hs/de`；4.2 `sharp_control` 的移位寄存器延迟链；4.3 数据通路与同步通路为什么要在 6 拍对齐。

---

### 4.1 视频时序信号 vs/hs/de

#### 4.1.1 概念说明

FPGA 收到的视频并不是“一张图”，而是一条**一维的像素流**：每个 74.25 MHz 时钟周期送来一个像素的 R/G/B。要把这条流重新摆成 1280×720 的二维画面，接收端必须知道：

- **现在这一拍是不是一个有效像素？** —— 由 `de`（Data Enable，数据有效）回答。`de='1'` 表示“此刻的 RGB 是画面里的一个真实像素，请采样”；`de='0'` 表示处于**消隐期**（blanking），RGB 无意义。
- **一行在哪里开始/结束？** —— 由 `hs`（Horizontal Sync，行同步）回答。它在每一行的特定位置产生一个脉冲，标记行边界。
- **一帧在哪里开始？** —— 由 `vs`（Vertical Sync，场同步）回答。它在每一帧的特定位置产生一个脉冲，标记帧边界。

为什么需要消隐期？早年 CRT 显示器每画完一行要回去下一行起点（行回扫）、画完一帧要回到左上角（场回扫），回扫期间不能显示像素。数字时代不再有 CRT，但这套“**有效区 + 消隐区**”的时序被标准保留下来，成为 720p 等格式的约定。

一句话记忆三者的关系：

```
一帧 = 多行；一行 = [消隐] + 若干有效像素(de='1') + [消隐]
vs 标帧界、hs 标行界、de 标“这一拍是有效像素”
```

对锐化滤波器而言，`de` 还有一个至关重要的副作用：它在 `sharp_slice` 内部被当作行存储的**写使能**（`write_en`），保证只有有效像素才被写进行缓存（u3-l1 的 4.2 节已提及）。所以 `de` 既是“排版信号”，又是“数据通路的使能信号”。

#### 4.1.2 核心流程

把一帧的传输画成时间线（横向是时钟周期，纵向是行）：

```
                de='1' 区(有效像素)        de='0' 区(行消隐)
行 0:   ... hs▕  P0  P1  P2 ... P1279  ▕  (回扫/前后肩) ...
行 1:   ... hs▕  P0  P1  P2 ... P1279  ▕  ...
        ...
行719:  ... hs▕  P0  P1  P2 ... P1279  ▕  ...
        (场消隐: 若干空行)  vs▕  ← 下一帧开始
```

要点：

- **`de` 是逐像素的**：它在每个有效像素上为 `'1'`，所以“一行内 `de` 的高电平段”恰好覆盖这一行的 1280 个有效像素。
- **`hs` 是逐行的**：每行一个脉冲，周期 = 一整行（含消隐）。
- **`vs` 是逐帧的**：每帧一个脉冲，周期 = 一整帧。
- 关键性质：**`de` 与 `hs` 都是“按行周期性重复”的信号**——它们的波形在每一行里长得几乎一样。这个性质是后面解释“行级延迟为何能被自动消化”的关键。

#### 4.1.3 源码精读

顶层实体声明里，三个输入与三个输出同步信号的注释写得非常清楚：[FPGA-Design/sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33)。摘录关键几行：

```vhdl
vs_in     : in  std_logic;     -- vertical sync
hs_in     : in  std_logic;     -- horizontal sync
de_in     : in  std_logic;     -- data enable is '1' for valid pixel
...
vs_out    : out std_logic;     -- corresponding to video-in
hs_out    : out std_logic;
de_out    : out std_logic;
```

注意输出侧注释 `corresponding to video-in`：输出同步信号要“对应”输入同步信号——只是被延迟到与输出像素对齐的位置。这正是 `sharp_control` 的职责。

`de_in` 还在输入进程里被打成 `de_0`：[sharp.vhd:58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L58) `de_0 <= de_in;`。这个 `de_0` 随后被**两路共用**：一路送进三个 `sharp_slice` 当写使能（[sharp.vhd:68/75/82](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L84)），另一路送进 `sharp_control` 当待延迟的同步信号（[sharp.vhd:92](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L86-L95)）。**两路从同一个 `de_0` 出发**，这是后面“对齐”得以成立的物理基础。

#### 4.1.4 代码实践（阅读型）

1. **目标**：确认三个同步信号的端口方向与注释，并理解 `de_0` 是“数据使能”与“待延迟同步”的共同源头。
2. **步骤**：
   - 打开 [sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33)，找出 `vs/hs/de` 的 `_in` 与 `_out` 各一对。
   - 在 [sharp.vhd:48-63](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L48-L63) 里定位 `de_0 <= de_in;`。
   - 搜索 `de_0`，确认它既出现在 `sharp_slice` 的 `port map`（`de_in => de_0`），也出现在 `sharp_control` 的 `port map`（`de_in => de_0`）。
3. **现象/预期**：`de_0` 至少被引用 4 次——3 个 `sharp_slice` 各 1 次、`sharp_control` 1 次。这说明同一个“是否有效”信号同时驱动数据通路的写使能与同步通路的延迟链。
4. 结论：同步信号并非独立的“配角”，它与像素数据共享同一个 `de_0` 锚点，二者的延迟必须严格匹配。

#### 4.1.5 小练习与答案

1. **问**：如果没有 `de`，接收端还能正确重建画面吗？
   **答**：会非常困难。`de` 明确告诉接收端“哪些周期是真实像素”。没有它，接收端只能靠精确数时钟周期来推断有效区，一旦行/帧长度有丝毫偏差就会错位。`de` 让有效性变得“自描述”，大幅降低了对齐难度。

2. **问**：`hs` 的脉冲频率与 `vs` 的脉冲频率，哪个更高？
   **答**：`hs` 高得多。`hs` 每行一个脉冲（720p 每帧约 750 行，故每帧约 750 个 `hs` 脉冲），而 `vs` 每帧才一个脉冲。所以 `hs` 频率 ≈ 行频（约几十 kHz），`vs` 频率 = 帧频（约 60 Hz）。

3. **问**：为什么本设计把 `de` 既当“同步信号”又当“写使能”？
   **答**：因为“这一拍是否是有效像素”这一信息，对同步通路（要不要把这一拍的 `de` 计入输出）和数据通路（要不要把这一拍的像素写进行缓存）是同一件事。复用同一个 `de_0` 既省资源，又天然保证两路在时间上严格一致。

---

### 4.2 移位寄存器延迟链（sharp_control 内部）

#### 4.2.1 概念说明

数据通路对像素造成了延迟（下一节会量化）。如果同步信号原样直通，就会和像素错位：`de_out` 喊“现在有效”时，此刻输出的像素其实是几拍之前的结果。解决办法很朴素——**把同步信号也延迟同样的拍数**，让对齐后的 `de_1` 与 `r_1/g_1/b_1` 出现在同一拍。

`sharp_control` 就是一个**可配置长度的延迟线**：它对 `vs/hs/de` 三个信号各维护一条移位寄存器，长度由 `generic delay` 决定，把每个信号原样延迟 `delay` 拍后输出。它**不做任何逻辑运算**，只负责“按时把信号往后挪”。

为什么用 `generic` 而不是写死？因为延迟拍数取决于数据通路深度（滤波器结构、寄存器级数），将来改算法时数据通路深度会变，用 `generic` 就能只改例化处的一行 `generic map`，而不必动模块内部。

#### 4.2.2 核心流程

`sharp_control` 的核心是一个被时钟驱动的移位：把当前输入放进数组第 1 格，其余各格逐格后移，输出取数组最后一格。

```
每个 clk 上升沿:
    vs_delay(1) <= vs_in            # 当前输入进第 1 格
    for i in 2 to delay:
        vs_delay(i) <= vs_delay(i-1) # 每格取上一格的旧值 → 整体后移一格
    vs_out <= vs_delay(delay)        # 最后一格作为输出
（hs、de 同理，三条独立链）
```

用一个具体例子追踪一拍输入 `X` 在 `delay=6` 时的旅程（“在第 k 拍可见”指该值在第 k−1 个上升沿被打入、从第 k 拍起稳定出现在该信号上）：

| 经过拍数 | vs_delay(1) | vs_delay(2) | vs_delay(3) | vs_delay(4) | vs_delay(5) | vs_delay(6)=vs_out |
| --- | --- | --- | --- | --- | --- | --- |
| 第 0 拍（输入 X） | — | — | — | — | — | — |
| 第 1 拍 | X | — | — | — | — | — |
| 第 2 拍 | · | X | — | — | — | — |
| 第 3 拍 | · | · | X | — | — | — |
| 第 4 拍 | · | · | · | X | — | — |
| 第 5 拍 | · | · | · | · | X | — |
| 第 6 拍 | · | · | · | · | · | **X** |

可见：**输入值 X 在第 0 拍出现于入口，第 6 拍出现于 `vs_out`**——延迟恰好等于 `delay` 拍。于是有一个直接结论：

\[ \text{sharp\_control 的延迟（拍数）} = \text{delay} \]

（`hs`、`de` 两条链完全同理，三条链并行、互不影响。）

#### 4.2.3 源码精读

先看实体声明——它带一个 generic `delay`，默认值是 **7**：[FPGA-Design/sharp_control.vhd:12-22](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L12-L22)

```vhdl
entity sharp_control is
  generic ( delay : integer := 7 );
  port ( clk    : in  std_logic; ...
         vs_in  : in  std_logic; hs_in : in std_logic; de_in : in std_logic;
         vs_out : out std_logic; hs_out: out std_logic; de_out: out std_logic);
end sharp_control;
```

注意默认 7，但顶层用 `generic map (delay => 6)` 覆盖成 6（见 4.3.3）。模块自身的默认值与顶层实际取值不同，这是常见做法——默认值只在不覆盖时生效。

接着看内部的三条数组与三个信号：[sharp_control.vhd:27-30](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L27-L30)

```vhdl
type delay_array is array (1 to delay) of std_logic;
signal vs_delay : delay_array;
signal hs_delay : delay_array;
signal de_delay : delay_array;
```

每个数组长度就是 `delay`，下标从 1 到 `delay`。这“一串 std_logic”综合后就是一排触发器——一条移位寄存器。

移位进程：[sharp_control.vhd:34-50](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L34-L50)

```vhdl
process
begin
  wait until rising_edge(clk);
  -- first value of array is current input
  vs_delay(1) <= vs_in;
  hs_delay(1) <= hs_in;
  de_delay(1) <= de_in;

  -- delay according to generic
  for i in 2 to delay loop
    vs_delay(i) <= vs_delay(i-1);
    hs_delay(i) <= hs_delay(i-1);
    de_delay(i) <= de_delay(i-1);
  end loop;
end process;
```

这里有两个值得注意的细节：

- **输入赋值放在进程内部**。文件头注释 [sharp_control.vhd:6](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L6) 写明：`assignment of input signals now inside process to avoid simulation problem, minimum delay is 2`。作者特意把 `vs_delay(1) <= vs_in` 放进时钟进程，是为了避免某些仿真器对“进程外连续赋值 + 进程内读”的处理差异；同时它声明**最小延迟为 2**（因为 `for i in 2 to delay` 要求 `delay >= 2`，否则循环范围为空、且首格语义不对）。
- **这是标准的同步移位**：进程内的 `<=` 都是非阻塞（signal 赋值），所以一拍之内 `vs_delay(i)` 读到的是**上一拍**的 `vs_delay(i-1)`，整体表现为“齐步后移一格”，而不是连锁塌陷。

最后，输出取数组末尾：[sharp_control.vhd:52-55](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L52-L55)

```vhdl
vs_out <= vs_delay(delay);
hs_out <= hs_delay(delay);
de_out <= de_delay(delay);
```

末尾格的内容就是 `delay` 拍前的输入，于是输出 = 输入延迟 `delay` 拍。

#### 4.2.4 代码实践（阅读 + 推理型）

1. **目标**：把“数组长度 = 延迟拍数”这一关系在脑子里跑通，并会预测任意 `delay` 的结果。
2. **步骤**：
   - 读 [sharp_control.vhd:34-55](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L34-L55)，确认三条链结构完全相同、互不影响。
   - 假设 `delay=4`，自己画一张类似 4.2.2 的表，确认 `X` 在第 4 拍到达 `vs_out`。
3. **现象/预期**：无论 `delay` 取多少，结论都是“输入在第 0 拍、输出在第 `delay` 拍”。延迟与 `delay` 严格相等。
4. 思考：如果误把 `delay` 设成 1，会怎样？`for i in 2 to 1 loop` 是空循环，于是只剩 `vs_delay(1) <= vs_in` 与 `vs_out <= vs_delay(1)`，相当于 1 拍延迟——但作者注释说最小应为 2，所以不要这么用。
5. 是否要真改源码：本实践只需阅读与推理，不必改源码（课程规则不允许改源码）。

#### 4.2.5 小练习与答案

1. **问**：`sharp_control` 里 `vs/hs/de` 三条链是共用一个数组，还是各有独立数组？
   **答**：各有独立数组（`vs_delay`、`hs_delay`、`de_delay`，见 [sharp_control.vhd:28-30](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L27-L30)）。三者延迟拍数相同（都是 `delay`），但内容互不干扰，保证三个信号被独立、同步地平移。

2. **问**：为什么循环写成 `for i in 2 to delay`，而不是 `for i in 1 to delay-1`？
   **答**：等价的，都表示“从第 2 格开始，每格取前一格”。作者用 `2 to delay` 是为了配合“第 1 格直接取输入”的写法，让“输入 → 第 1 格 → … → 第 delay 格 → 输出”这条链读起来更直观。

3. **问**：如果把 `vs_delay(i) <= vs_delay(i-1)` 写成变量赋值（`:=`）会怎样？
   **答**：会破坏移位语义。变量赋值是立即生效的，第 `i` 格会立刻拿到第 `i-1` 格的新值，连锁传递导致一拍之内整条链都变成同一个值（塌陷），延迟就消失了。这正是为什么移位寄存器必须用 signal 的非阻塞 `<=`。

---

### 4.3 数据/同步对齐：为什么顶层传入的是 6

#### 4.3.1 概念说明

这一节是本讲的核心。要回答“为什么是 6”，必须先把数据通路的延迟拆成**两种尺度**：

1. **行内延迟（以时钟周期计）**：像素在 `sharp_slice` 内部经过的流水线触发器造成的、按“拍”计的延迟。这部分会改变像素在**一行之内**的相位（往后挪几个像素位置）。
2. **行级延迟（以行为单位计）**：垂直滤波需要同一列上下几行的像素，于是用行存储把数据“压住”几整行（本项目约 3 行）。这部分改变像素的**行号**，但在一行之内的相位不变。

对齐同步信号时，这两类延迟要分开处理：

- **行级延迟**（约 3 行）：因为 `de` 和 `hs` 都是“按行周期性重复”的信号（4.1.2 已强调），把数据往后挪整数行，等价于把 `de`/`hs` 往后挪整数个周期——而周期性信号挪整数个周期后**相位不变**。所以行级延迟对 `de`/`hs` 的“行内相位”是**透明的**，不需要 `sharp_control` 去补偿。（它会造成整帧图像向下平移约 3 行，这一垂直偏移由 u5-l2 的自校验测试台在比对时单独补偿。）
- **行内延迟（若干拍）**：这才是 `sharp_control` 必须精确补偿的部分——把 `de`/`hs`/`vs` 在一行之内往后挪同样的拍数，让 `de_out='1'` 的那一拍正好对应一个已经锐化好的有效像素。

所以问题收敛为：**`sharp_slice` 的“行内延迟”是多少拍？`sharp_control` 的 `delay` 就该取多少。**

#### 4.3.2 核心流程

把两条通路“从同一个输入进程到同一个输出进程”的行内延迟列出来比较。注意：`de_0` 与 `r_0` 都在 [sharp.vhd:48-63](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L48-L63) 的输入进程里被打一拍，所以它们在**第 1 拍是对齐的**；`de_out` 与 `r_out` 又都在 [sharp.vhd:98-116](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L98-L116) 的输出进程里被一起打一拍，所以只要中间段（`de_0→de_1` 与 `r_0→r_1`）延迟相等，出口就重新对齐。

**同步通路 `de_0 → de_1`**：只经过 `sharp_control`，行内延迟 = `delay` 拍。

**数据通路 `r_0 → r_1`（即 `sharp_slice` 的行内延迟）**，逐级数（仅数行内拍数，不计 3 行垂直延迟）：

| 级 | 位置 | 行内拍数 |
| --- | --- | --- |
| 垂直 `sharp_arith`（寄存器输出） | [sharp_slice.vhd:39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49) | +1 |
| 水平抽头 `h_tap(0) <= v_out`（寄存器） | [sharp_slice.vhd:51-58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58) | +1 |
| 水平中心对齐：`h_tap(3)` 落后 `h_tap(0)` | 同上 | +3 |
| 水平 `sharp_arith`（寄存器输出） | [sharp_slice.vhd:60-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70) | +1 |
| **合计 `sharp_slice` 行内延迟** | | **6** |

于是两条通路的“完整行内延迟”（含输入/输出进程各 1 拍）为：

\[
\text{同步通路} = \underbrace{1}_{de_0} + \underbrace{\text{delay}}_{sharp\_control} + \underbrace{1}_{de\_out}
\]

\[
\text{数据通路} = \underbrace{1}_{r_0} + \underbrace{6}_{sharp\_slice\,\text{行内}} + \underbrace{1}_{r\_out}
\]

两者要相等，必须有：

\[
1 + \text{delay} + 1 \;=\; 1 + 6 + 1 \quad\Longrightarrow\quad \text{delay} = 6
\]

**这就是顶层传入 `delay => 6` 的根本原因**：`sharp_control` 的延迟必须等于 `sharp_slice` 的行内延迟（6 拍），如此 `de_out` 与 `r_out` 才会在同一拍出现。多出的“3 行垂直延迟”因为 `de`/`hs` 的行周期性而对行内相位透明，不在 `delay` 里体现。

> 一句话总结：`delay=6` 不是经验值，而是 `sharp_slice` 行内流水线深度决定的——改了滤波器结构（增减一级寄存器），就得相应调整这个 6。

#### 4.3.3 源码精读

顶层例化处，`generic map` 把延迟设为 6：[FPGA-Design/sharp.vhd:86-95](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L86-L95)

```vhdl
control: entity work.sharp_control
    generic map (delay => 6) 
    port map (  clk    => clk,
                reset  => reset,
                vs_in  => vs_0, hs_in => hs_0, de_in => de_0,
                vs_out => vs_1, hs_out=> hs_1, de_out=> de_1);
```

它的三个输入接 `vs_0/hs_0/de_0`（输入进程的产出），三个输出 `vs_1/hs_1/de_1` 进输出进程。

数据通路侧，验证 `sharp_slice` 行内 = 6 拍所引用的三处寄存器：

- 垂直 `sharp_arith` 的寄存器输出：[sharp_arith.vhd:31-45](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L29-L45)（进程 `wait until rising_edge(clk)` 后才给 `data_out` 赋值，故 +1 拍）。
- 水平抽头进程：[sharp_slice.vhd:51-58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58)，`h_tap(0) <= v_out`（+1 拍），随后 `h_tap(i+1) <= h_tap(i)` 把值逐拍后移，中心抽头 `h_tap(3)` 比 `h_tap(0)` 晚 3 拍（+3）。
- 水平 `sharp_arith`：同样是寄存器输出（+1 拍）。

最后，输出进程把同步与数据“同进程打拍”，完成出口对齐：[sharp.vhd:104-110](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L104-L110)

```vhdl
vs_out  <= vs_1;
hs_out  <= hs_1;
de_out  <= de_1;
r_out   <= std_logic_vector(to_unsigned(r_1,8));
g_out   <= std_logic_vector(to_unsigned(g_1,8));
b_out   <= std_logic_vector(to_unsigned(b_1,8));
```

`de_1` 与 `r_1/g_1/b_1` 在**同一个进程**里被一起打一拍送出——只要它们进入这个进程时已经对齐（由 `delay=6` 保证），出口 `de_out` 与 `r_out` 就严格同拍。

#### 4.3.4 代码实践（动手验证型）

这是本讲的主实践：用实验确认“`delay` 必须等于 6”，并解释偏离时的现象。在**自己的工程副本**上做（课程规则不允许改课程仓库源码）。

1. **目标**：把 `sharp_control` 的 `delay` 改成 3 与 9，观察 `de_out` 与输出像素是否仍然对齐，并用对齐公式解释现象。
2. **操作步骤**：
   - 复制一份工程，定位 [sharp.vhd:87](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L86-L95) 的 `generic map (delay => 6)`。
   - 改成 `delay => 3`，用 u5-l1 的 `sim_sharp` 仿真（先按 u5-l1 把硬编码路径改成你本地的 PPM 路径），观察波形里 `de_out` 相对有效输出像素 `r_out` 的位置；或用 u5-l2 的自校验测试台跑一遍，记录 mismatch 计数。
   - 再改成 `delay => 9`，重复。
   - 最后改回 6，确认 mismatch 归零、报告通过。
3. **需要观察的现象**：
   - `delay=3` 时，同步通路行内延迟 = 1+3+1 = 5 拍，比数据通路的 8 拍**早 3 拍**。于是 `de_out` 会提前 3 拍喊“有效”，导致每行**开头约 3 个“有效”位置对应的其实是尚未锐化好/无效的像素**，行尾的 3 个真实有效像素反而被切到消隐期。
   - `delay=9` 时，同步通路 = 1+9+1 = 11 拍，比数据通路**晚 3 拍**，现象相反：`de_out` 滞后，每行开头约 3 个已锐化像素被漏标、行尾多出 3 个“无效却标有效”的位置。
   - 两种情况下，自校验测试台的逐像素比对都会失配，mismatch 计数明显大于 0。
4. **预期结果**：`delay=6` 时 mismatch 归零（图像比对通过）；`delay=3` 与 `delay=9` 都会出现水平方向的边缘错乱与 mismatch 增多，且偏离量都是 3 拍（与 `|新值−6|` 一致）。**待本地验证**：精确的 mismatch 数目取决于测试图边缘内容，建议以实际仿真为准。
5. **解释**：偏离 `delay=6` 就是让同步通路行内延迟 ≠ 数据通路行内延迟，`de_out` 与 `r_out` 错位若干拍。只有 `delay=6` 才满足 \(1+\text{delay}+1 = 1+6+1\)，二者重新对齐。

#### 4.3.5 小练习与答案

1. **问**：如果把 `sharp_arith` 内部那一级寄存器去掉（让乘加变成纯组合输出），`delay` 还应是 6 吗？
   **答**：不应。垂直与水平两个 `sharp_arith` 各少 1 拍，`sharp_slice` 行内延迟从 6 变成 4。此时 `delay` 也应相应改成 4 才能重新对齐。这正说明 `delay` 是被数据通路深度“推导”出来的，不是固定常数。

2. **问**：行级延迟约 3 行，为什么不需要在 `sharp_control` 里也补偿这 3 行？
   **答**：因为 `de`、`hs` 是按行周期性重复的信号，延迟整数个“行周期”不改变它们在一行之内的相位。所以 3 行的垂直延迟对 `de`/`hs` 的行内对齐是透明的，无需补偿。它只会让整帧图像向下平移约 3 行，这一偏移在 u5-l2 的自校验测试台里被单独补偿。

3. **问**：`vs`（场同步）也只延迟 6 拍，那 3 行的垂直延迟会不会让 `vs_out` 相对画面偏移？
   **答**：会。`vs` 是按帧周期重复的，3 行的延迟会让 `vs_out` 在帧内向下偏移约 3 行（与画面本身的 3 行下移一致）。对“画面 + 同步一起平移”的视频链路而言，这种整体平移通常可接受；本设计也正是如此处理的，并在仿真比对时补偿这 3 行。

---

## 5. 综合实践：在仿真里“实测”对齐拍数

这个任务把本讲三个模块串起来：用仿真验证“`delay` 必须等于 `sharp_slice` 行内延迟”，并把 4.3 的静态推导与实际波形对上。

### 实践目标

在自校验测试台里，分别用 `delay = 6 / 5 / 7` 三个值跑仿真，确认只有 6 能让 `de_out` 与输出像素对齐（mismatch 归零），并解释 ±1 偏离时的现象，从而“实测”出对齐拍数。

### 操作步骤

1. 准备测试图与期望图：按 u5-l3，用 `sharp_generate_testbench_images.m` 对一张 1280×720 测试图生成输入 PPM 与 expected PPM。
2. 在工程副本里把 [sharp.vhd:87](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L86-L95) 的 `delay` 设为 6，跑 u5-l2 的自校验测试台 `sim_sharp_self-checking.vhd`，确认报告通过、mismatch = 0。
3. 依次改成 5、再改成 7，各跑一次，记录 mismatch 计数与输出 PPM 的边缘表现。
4. （可选）在波形里把 `de_out` 与 `r_out` 拉到一起，肉眼确认 `delay=6` 时二者同拍起落、`delay=5/7` 时错开 1 拍。

### 需要观察的现象 / 预期结果

| `delay` | 同步行内延迟 | 与数据通路(8)相比 | 预期 mismatch | 现象 |
| --- | --- | --- | --- | --- |
| 5 | 1+5+1 = 7 | 早 1 拍 | > 0 | 每行起始约 1 像素错位 |
| **6** | **1+6+1 = 8** | **相等** | **0** | **对齐，比对通过** |
| 7 | 1+7+1 = 9 | 晚 1 拍 | > 0 | 每行末尾约 1 像素错位 |

**待本地验证**：mismatch 的具体数字依赖测试图边缘像素内容，上表“> 0”是定性结论，精确数值以实际仿真为准。

### 结论

只有当 `delay` 恰好补偿 `sharp_slice` 的行内延迟（6 拍）时，`de_out` 才与输出像素同拍、自校验通过。这个实验把 4.3 的公式 \(1+\text{delay}+1 = 1+6+1\) 从纸面搬进了波形，并直观展示了“同步延迟必须匹配数据延迟”这一视频处理的基本纪律。

## 6. 本讲小结

- 视频用 `vs`（场同步）、`hs`（行同步）、`de`（数据有效）三个信号把一维像素流排版成二维画面；`de='1'` 标记有效像素，`hs`/`vs` 标记行/帧边界。
- `de` 在本设计中身兼二职：既是同步信号，又是 `sharp_slice` 行存储的写使能；两路从同一个 `de_0` 出发，是对齐的物理基础。
- `sharp_control` 是一个可配置长度的移位寄存器延迟链，把 `vs/hs/de` 各原样延迟 `delay` 拍后输出；其延迟严格等于 `delay`（默认 7，顶层覆盖为 6）。
- 数据通路延迟分两类：**行级延迟**（约 3 行，来自垂直行存储）与**行内延迟**（6 拍，来自 `sharp_slice` 流水线寄存器）。前者因 `de`/`hs` 行周期性而对行内相位透明，无需补偿；后者正是 `sharp_control` 要精确补偿的部分。
- **顶层传入 `delay => 6`，是因为 `sharp_slice` 的行内延迟恰为 6 拍**：同步通路 \(1+\text{delay}+1\) 必须等于数据通路 \(1+6+1\)，解得 `delay=6`。
- 若把 `delay` 改成 3 或 9，`de_out` 会与输出像素错位若干拍，自校验 mismatch 增多；这反向印证了 6 的必要性。

## 7. 下一步学习建议

- **u3-l3 sharp_slice 的二维滤波数据流**：本讲把 `sharp_slice` 的“行内 6 拍”当成结论，下一讲打开它，精读 6 个 `sharp_linemem` 如何级联出 7 个垂直抽头、水平移位如何出 7 个水平抽头、两次 `sharp_arith` 如何串成可分离二维滤波，你会对那 6 拍的每一拍都“对得上号”。
- **U4 核心模块**：`sharp_linemem` 的 1280 项循环缓冲（u4-l1，解释“一行延迟”如何实现）与 `sharp_arith` 的定点乘加/饱和截断（u4-l2，解释那两级寄存器输出）。
- **U5 仿真验证**：u5-l1 的 PPM 测试台是本讲实践的运行环境；u5-l2 的自校验测试台则展示了“3 行垂直偏移”是如何被补偿的，正好与本讲的行级延迟分析呼应。
- 阅读建议：把本讲的“对齐公式”与 u3-l1 的“像素链路表”并排看——前者解释了同步侧，后者解释了数据侧，两张图合起来就是完整的视频流水线对齐全景。
