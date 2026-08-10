# 顶层 sharp.vhd：视频输入输出与模块例化

## 1. 本讲目标

本讲进入硬件实现的进阶层，从**顶层** `sharp.vhd` 开始精读。读完本讲，你应当能够：

- 看懂顶层 `sharp.vhd` 的端口（视频输入/输出、时钟、复位、使能）和它在做“类型转换 + 模块例化”的边界作用；
- 解释为什么对 R / G / B 三个颜色分量**各例化一个** `sharp_slice`，以及这种并行结构的代价与好处；
- 理清顶层到子模块（`sharp_slice`、`sharp_control`）的端口映射关系；
- 沿着 `r_in → r_out` 追踪一个像素，列出它经过的**寄存器级数**和**延迟来源**。

本讲只聚焦顶层；`sharp_slice` 内部二维滤波的细节是下一讲（u3-l3）的内容，这里先把它当成一个“把 `data_in` 二维锐化后输出 `data_out`”的黑盒。

## 2. 前置知识

本讲承接两篇前置讲义，不再重复其结论，只直接使用：

- **u1-l2 仓库目录结构**：你已经知道 `sharp` 是顶层，例化了 3 个 `sharp_slice`（分处 R/G/B）和 1 个 `sharp_control`；`sharp_slice` 再例化 `sharp_linemem` 与 `sharp_arith`。本讲把这张“例化层次图”展开成具体的端口连线。
- **u2-l1 图像锐化与可分离 FIR**：锐化核为 \([1,0,-9,48,-9,0,1]/32\)，二维滤波等价于“先垂直、再水平”两次一维卷积。顶层的三个 `sharp_slice` 就是在硬件上对每个颜色通道做这件事。

此外需要一点 VHDL 基础概念（不熟悉的术语下面会解释）：

- **实体（entity）与架构（architecture）**：实体声明“对外长什么样（端口）”，架构描述“内部怎么实现”。
- **信号（signal）与寄存器**：在时钟进程（`wait until rising_edge(clk)`）里给 `signal` 赋值，综合后通常得到一个触发器（寄存器）；进程外的连续赋值则更像连线（组合逻辑）。
- **`std_logic_vector` 与 `integer`**：前者是一串比特，本身不表示“数值”；后者是真正的整数。做乘加、比较时用 `integer` 更直观。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `FPGA-Design/sharp.vhd` | **顶层**：端口边界、输入/输出寄存器、类型转换、模块例化 | 本讲主角，逐段精读 |
| `FPGA-Design/sharp_slice.vhd` | 单颜色通道的二维可分离锐化 | 当作黑盒，只看它的端口；内部细节留到 u3-l3 |
| `FPGA-Design/sharp_control.vhd` | 把 `vs/hs/de` 同步信号延迟若干拍，与数据对齐 | 精读它的端口与移位原理；拍数选择的细节留到 u3-l2 |
| `FPGA-Design/sharp_arith.vhd` | 7 抽头定点乘加 + 饱和截断（U4 详讲） | 追踪延迟时只关心“它有一个寄存器输出” |
| `FPGA-Design/sharp_linemem.vhd` | 1280 项行存储循环缓冲（U4 详讲） | 追踪延迟时只关心“它延迟一整行像素” |

> 提示：本讲引用了 `sharp_arith.vhd` 和 `sharp_linemem.vhd` 的少量信息（仅用于解释延迟来源），它们的完整精读分别在 u4-l2 与 u4-l1。

## 4. 核心概念与源码讲解

先给一张顶层数据流总览，后续三个最小模块分别对应图中的三段：

```
                 ┌──────────────────────────────────────────────────┐
 clk ───────────►│                                                  │
 reset_n ───────►│  ① 输入寄存器  (std_logic_vector ──► integer)     │
 vs/hs/de_in ───►│        │                                         │
 r_in/g_in/b_in►│        ├──► r_slice ──┐                           │
                 │        ├──► g_slice ──┤  r_1/g_1/b_1 (integer)    │
                 │        ├──► b_slice ──┘                           │
                 │        └──► sharp_control(delay=6) ── vs_1/hs_1/de_1
                 │                    │                              │
                 │  ③ 输出寄存器 (integer ──► std_logic_vector)       │
 vs/hs/de_out ◄──┤◄───────────────────┘                              │
 r_out/g_out/b_out◄─┤                                                 │
 clk_o ◄─────────┤ (clk 直通，注释 "do not modify")                   │
 led ◄───────────┤ ("000")                                           │
                 └──────────────────────────────────────────────────┘
        ② 三通道并行例化（RGB 各一个 sharp_slice）
```

三个最小模块分别是：① 输入寄存器与类型转换；② RGB 三通道并行例化；③ 输出寄存器与同步。

---

### 4.1 输入寄存器与类型转换

#### 4.1.1 概念说明

顶层最核心的角色是“**边界适配器**”：它把来自外部的硬件信号（`std_logic` / `std_logic_vector`）整理成内部更好用的形式，再把内部结果整理回外部需要的格式。

为什么要专门加一级**输入寄存器**？

1. **切断外部组合路径，改善时序**：外部信号可能经过较长的走线才到达本模块，先打一拍寄存器，后续逻辑的起点就是一个干净、对本地时钟而言很“近”的信号。
2. **同步到本模块时钟域**：把异步到达的输入对齐到本地 `clk` 的上升沿，减少亚稳态风险。
3. **在边界集中做类型转换**：把像素从 `std_logic_vector` 转成 `integer`，让内部 `sharp_arith` 里的乘加、比较、饱和写起来像普通算术。

为什么内部用 `integer range 0 to 255`？像素灰度本身就是 0~255。用 `integer` 后，`48 * tap_00 - 9 * tap_m1` 这样的表达式可以直接写，可读性远高于在 `std_logic_vector` 上手动做算术。综合器会根据取值范围推断出合适的位宽，并不会真的浪费资源。

#### 4.1.2 核心流程

每个 `clk` 上升沿，输入寄存器进程把所有输入信号“打一拍”：

- `reset_n` 取反得到内部高有效 `reset`（子模块统一用高有效复位，所以在边界统一翻转极性）；
- `enable_in` 采样进 `enable`（注意：**本版本里 `enable` 被采样后并没有用来门控任何逻辑**，滤波始终在运行，它更像一个预留接口）；
- `vs_in / hs_in / de_in` 采样成 `vs_0 / hs_0 / de_0`；
- `r_in / g_in / b_in`（`std_logic_vector`）通过 `to_integer(unsigned(...))` 转成 `r_0 / g_0 / b_0`（`integer 0..255`）。

伪代码：

```
每个上升沿:
    reset  <= not reset_n          # 极性翻转
    enable <= enable_in            # 预留，当前未使用
    vs_0   <= vs_in
    hs_0   <= hs_in
    de_0   <= de_in
    r_0    <= to_integer(unsigned(r_in))   # 比特串 -> 整数
    g_0    <= to_integer(unsigned(g_in))
    b_0    <= to_integer(unsigned(b_in))
```

#### 4.1.3 源码精读

先看实体端口声明，建立“顶层对外长什么样”的全貌：[FPGA-Design/sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33) —— 注释写明 `clk` 是 74.25 MHz / 720p 视频时钟，`de_in` 为 `'1'` 表示有效像素，`r/g/b_in` 各 8 位；输出侧有对应的 `vs/hs/de_out` 与 `r/g/b_out`，外加 `clk_o`（输出时钟）和 `led`。

接着看输入寄存器进程：[FPGA-Design/sharp.vhd:48-63](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L48-L63) —— 这一段就是上面伪代码的 VHDL 原文。关键三行类型转换：

```vhdl
r_0   <= to_integer(unsigned(r_in)); 
g_0   <= to_integer(unsigned(g_in));
b_0   <= to_integer(unsigned(b_in));
```

`unsigned(r_in)` 把 `std_logic_vector` 重新解释为“无符号数”，`to_integer(...)` 再转成 `integer`。两步缺一不可：`std_logic_vector` 在 IEEE 标准里只是比特串、不是数，不能直接 `to_integer`。

`enable` 信号只在这两处出现——声明 [sharp.vhd:39](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L39) 与赋值 [sharp.vhd:54](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L54)，全文再无引用，且 `sharp_slice`/`sharp_control` 都没有使能端口。所以它是“采了样却没接线”的预留信号，记住这点有助于后面理解“滤波器一上电就在持续处理”。

#### 4.1.4 代码实践（阅读型）

1. **目标**：确认输入寄存器做了两件事——打一拍 + 类型转换。
2. **步骤**：打开 [sharp.vhd:48-63](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L48-L63)；用编辑器在文件里搜索 `enable`，确认它除了第 39、54 行外确实没有被使用。
3. **现象/预期**：你会看到 `enable` 没有任何“消费者”，说明当前设计没有用它来开关滤波；`reset` 则会出现在每个子模块的端口映射里（它是真正被使用的控制信号）。
4. 结论：输入寄存器的“有效产出”是 `reset`、`vs_0/hs_0/de_0`、`r_0/g_0/b_0` 这几路，`enable` 仅作预留。

#### 4.1.5 小练习与答案

1. **问**：为什么内部写成 `reset <= not reset_n;`，而不是直接把 `reset_n` 传给子模块？
   **答**：子模块（`sharp_slice`、`sharp_control`、`sharp_arith`、`sharp_linemem`）统一使用**高有效**复位；而 FPGA 的外部复位常是低有效（配置期间拉低）。顶层在边界做一次极性翻转，让内部全部用同一种极性，避免每个子模块各自翻转。

2. **问**：`to_integer(unsigned(r_in))` 里的 `unsigned(...)` 能不能省略？
   **答**：不能。`std_logic_vector` 在 `numeric_std` 里被当作“无数学含义的比特串”，`to_integer` 只接受 `unsigned`/`signed`。必须先用 `unsigned()` 重解释，否则编译报错。

3. **问**：把 `r_0` 声明成 `integer range 0 to 255` 而不是更大的范围，有什么好处？
   **答**：限定范围后，综合器知道这个值最多 8 位，会推断出 8 位寄存器，节省资源；同时 `r_0` 的语义就是“像素灰度”，可读性更好。

---

### 4.2 RGB 三通道并行例化

#### 4.2.1 概念说明

彩色视频的每个像素由 R、G、B 三个分量组成。锐化本质上是对“亮度变化”的增强，但本项目的做法非常直白：**对 R、G、B 三个分量分别、独立地做完全相同的二维锐化**。体现在硬件上，就是顶层例化了三个一模一样的 `sharp_slice`，分别处理红、绿、蓝。

为什么要“**三个独立实例**”而不是“时分复用一个”？

- **结构清晰**：每条通道一条数据流，互相没有共享状态，读代码、调参数都简单。
- **吞吐高**：三个通道同时处理，每个时钟周期就能产出一个完整的 RGB 像素，匹配 720p 的实时像素率。
- **代价是面积**：行存储等资源要 ×3。对 720p 这点资源是可以接受的（u1-l3 提到片上存储约占 16%）。

这是一种典型的“**用面积换吞吐**”的取舍。三个实例共享同一个 `clk`、`reset`、`de_0`，但 `data_in`/`data_out` 各自独立。

#### 4.2.2 核心流程

数据通路（本讲把 `sharp_slice` 当黑盒）：

```
r_0 ──► r_slice ──► r_1   (红通道二维锐化)
g_0 ──► g_slice ──► g_1   (绿通道二维锐化)
b_0 ──► b_slice ──► b_1   (蓝通道二维锐化)
共同输入: clk, reset, de_0  (de_0 用于控制 sharp_slice 内部行存储的写使能)
```

`de_0` 被三个实例共用，是因为“当前像素是否有效”对三个通道在同一位置完全一致——有效就一起有效，消隐就一起消隐。`de_0` 进入 `sharp_slice` 后被当作行存储的写使能（`write_en`），保证只在有效像素上推进行缓存。

> 关于 `sharp_slice` 内部：它先用 6 个 `sharp_linemem` 级联出 7 个**垂直抽头**送入第一个 `sharp_arith`（垂直滤波），再用一组水平移位寄存器出 7 个**水平抽头**送入第二个 `sharp_arith`（水平滤波）。这正是 u2-l1 讲的“可分离二维滤波”的硬件实现，细节在 u3-l3。

#### 4.2.3 源码精读

三个例化结构完全相同，差别只在 `data_in`/`data_out` 绑到哪一路。红通道：[FPGA-Design/sharp.vhd:65-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L70)；绿通道：[sharp.vhd:72-77](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L72-L77)；蓝通道：[sharp.vhd:79-84](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L79-L84)。以红通道为例：

```vhdl
r_slice: entity work.sharp_slice 
    port map (  clk      => clk,
                reset    => reset,
                de_in    => de_0,
                data_in  => r_0,
                data_out => r_1);
```

对照 `sharp_slice` 的实体端口 [FPGA-Design/sharp_slice.vhd:10-16](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L10-L16)：它的 `data_in`/`data_out` 正是 `integer range 0 to 255`——这就是为什么顶层要把 `std_logic_vector` 转成 `integer`，否则端口对不上。`de_in` 在 `sharp_slice` 内部连到每个 `sharp_linemem` 的 `write_en`，见 [sharp_slice.vhd:27-36](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L27-L36)。

#### 4.2.4 代码实践（阅读 + 思考型）

1. **目标**：体会“三通道结构对称、仅数据来源不同”。
2. **步骤**：对比 [sharp.vhd:65-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L70) 与 [sharp.vhd:79-84](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L79-L84)，找出两处差异——只有例化名（`r_slice`/`b_slice`）和 `data_in`/`data_out` 绑定的信号（`r_0/r_1` vs `b_0/b_1`）不同。
3. **思考**：如果把 `g_slice` 的 `data_in` 故意从 `g_0` 改成 `r_0`（绿通道也喂入红通道数据），输出图像颜色会怎样？
4. **预期**：绿通道输出会变成“红数据的锐化结果”，整幅图偏红/偏色——这验证了三通道是彼此独立的处理路径。
5. 是否要真改源码：本实践**只需阅读和推理**，不必修改源码（本课程规则不允许改源码）。若想验证，可在自己的副本上试验。

#### 4.2.5 小练习与答案

1. **问**：三个 `sharp_slice` 是否共享同一组行存储 RAM？
   **答**：不共享。每个 `sharp_slice` 内部各有 6 个独立的 `sharp_linemem`，所以三通道合计 18 个行存储。这正是“面积换吞吐”的代价来源。

2. **问**：为什么三个实例的 `de_in` 都接 `de_0`，而不是各接一份？
   **答**：`de` 表示“当前像素有效”，对 R/G/B 在同一像素位置完全相同。共用一份 `de_0` 既省资源，也保证三个通道在同一拍一起推进、严格同步。

3. **问**：如果未来想“只对亮度锐化、不动色度”，顶层的这种三实例结构还合适吗？
   **答**：不太合适。三实例结构假设三个分量对称处理；若要按亮度/色度分开处理，需要先在顶层做 RGB→YCbCr 转换、只对 Y 通道锐化、再转回 RGB，结构会显著变复杂。本设计的简洁正来自“三通道对称”这一前提。

---

### 4.3 输出寄存器与同步

#### 4.3.1 概念说明

经过 `sharp_slice` 后，数据通路对像素造成了**延迟**（行存储 + 流水线寄存器，下一节会量化）。可是视频同步信号 `vs`（场同步）、`hs`（行同步）、`de`（数据有效）如果原样直接输出，就会和像素**错位**：`de_out` 喊“现在这个像素有效”，可此刻输出的像素其实是几行之前的结果。

解决办法是用 `sharp_control` 把 `vs/hs/de` 也**延迟同样的拍数**，让对齐后的 `de_1` 与 `r_1/g_1/b_1` 出现在同一拍。最后再由输出寄存器把 `integer` 转回 `std_logic_vector` 送出去。

#### 4.3.2 核心流程

两条通路在输出端汇合：

```
数据:   r_1/g_1/b_1 (integer) ──► 输出寄存器(类型转换) ──► r_out/g_out/b_out (std_logic_vector)
同步:   vs_0/hs_0/de_0 ──► sharp_control(delay=6) ──► vs_1/hs_1/de_1 ──► 输出寄存器 ──► vs_out/hs_out/de_out
其他:   clk_o <= clk   (随路时钟直通，注释 "do not modify")
        led   <= "000" (LED 恒灭，注释 "not supported by remote lab")
```

`sharp_control` 内部是一个长度为 `delay` 的**移位寄存器**：把输入信号逐拍往后挪，取数组末尾作为输出，从而实现“固定拍数延迟”。

#### 4.3.3 源码精读

`sharp_control` 的例化：[FPGA-Design/sharp.vhd:86-95](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L86-L95) —— 注意 `generic map (delay => 6)`，顶层把延迟设为 6 拍。

`sharp_control` 内部原理：实体声明带一个 generic [FPGA-Design/sharp_control.vhd:12-22](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L12-L22)（默认值是 7，但顶层覆盖成 6）；它声明三个长度为 `delay` 的数组 [sharp_control.vhd:27-30](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L27-L30)；进程里把当前输入放进数组第 1 格、其余逐格后移 [sharp_control.vhd:34-50](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L34-L50)；输出取数组最后一格 [sharp_control.vhd:52-55](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L52-L55)。核心移位片段：

```vhdl
vs_delay(1) <= vs_in;                 -- 当前输入进第 1 格
for i in 2 to delay loop
  vs_delay(i) <= vs_delay(i-1);       -- 逐格后移
end loop;
...
vs_out <= vs_delay(delay);            -- 最后一格作为输出
```

输出寄存器进程：[FPGA-Design/sharp.vhd:98-116](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L98-L116)，把同步信号和像素一起再打一拍，同时做反向类型转换：

```vhdl
r_out   <= std_logic_vector(to_unsigned(r_1,8));
```

`to_unsigned(r_1, 8)` 把整数转回 8 位无符号数，`std_logic_vector(...)` 再重解释成比特串输出。

**一个容易被忽略的细节**：[sharp.vhd:107-115](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L107-L115) 有一段被注释掉的代码，原本的逻辑是“`if de_1='1'` 才输出像素、否则输出 0”。当前版本去掉了这个门控，**消隐期也照样把 `r_1/g_1/b_1` 送出去**。这没问题，因为下游视频接收端只会根据 `de_out='1'` 采样像素，消隐期的 RGB 值不会被采纳。

最后两行常量赋值：`clk_o <= clk`（[sharp.vhd:118](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L118)，注释“do not modify”，它给下游提供与数据同源的随路时钟）；`led <= "000"`（[sharp.vhd:119](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L119)，LED 恒灭）。

#### 4.3.4 代码实践（阅读 + 思考型）

1. **目标**：理解“同步信号必须跟着数据一起延迟”，以及输出寄存器里的反向类型转换。
2. **步骤**：
   - 读 [sharp_control.vhd:34-55](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L34-L55)，确认它就是把 `vs/hs/de` 各延迟 `delay` 拍。
   - 读 [sharp.vhd:104-110](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L104-L110)，确认 `vs_1/hs_1/de_1` 与 `r_1/g_1/b_1` 在**同一个进程**里被一起打一拍——这正是“输出端对齐”的关键。
3. **思考**：如果完全去掉 `sharp_control`（把 `vs_0` 直接连到 `vs_out`），会出现什么问题？
4. **预期**：同步信号会提前若干拍出现，`de_out` 标记“有效”时对应的像素其实是更早/更晚的结果，图像会出现整体偏移或边缘错乱。精确的“为什么是 6 拍”在 u3-l2 详讲。
5. 若想观察：可在自校验仿真（u5-l2）中临时把 `delay` 改大/改小，看 mismatch 计数变化（在自己的副本上做）。

#### 4.3.5 小练习与答案

1. **问**：`sharp_control` 的 generic `delay` 默认值是多少？顶层实际用的是多少？
   **答**：默认是 7（见 [sharp_control.vhd:13](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_control.vhd#L13)）；顶层通过 `generic map (delay => 6)` 覆盖成 6。

2. **问**：`clk_o` 的注释为什么写“do not modify”？
   **答**：`clk_o <= clk` 给下游视频接收器件提供与输出数据同源的“随路时钟”。改了它（比如分频、反相）会破坏源同步时序关系，导致下游采样错位。

3. **问**：输出进程里被注释掉的 `if de_1='1'` 门控，去掉后为什么仍然正确？
   **答**：因为下游只在 `de_out='1'` 时采样像素，消隐期输出的 RGB 不会被使用；强行在消隐期输出 0 只是把“不用的值”变成确定的 0，对最终图像没有影响，所以注释掉不影响正确性。

---

## 5. 综合实践：追踪一个像素从 `r_in` 到 `r_out`

这是一个贯穿本讲全部三个模块的任务：把红通道的一个像素值，从进入顶层到输出顶层，**走过的每一级寄存器和每一处延迟来源**列出来。它能把“输入寄存器 → 三通道例化 → 输出寄存器”串成一条完整的链路。

### 实践目标

画出 `r_in → r_out` 的寄存器/延迟链路图，区分“时钟周期的流水线寄存器”和“行级延迟”，并指出同步信号是如何被对齐的。

### 操作步骤

1. **起点（模块①）**：从 [sharp.vhd:59](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L59) `r_0 <= to_integer(unsigned(r_in));` 开始——这是第 1 级寄存器（兼做类型转换）。
2. **进入黑盒（模块②）**：沿 `r_slice` 例化 [sharp.vhd:65-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L70) 进入 `sharp_slice`，在 [sharp_slice.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd) 内部依次定位：
   - `v_tap(0) <= data_in;`（[L27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L27)）：**组合连线，0 级寄存器**。
   - 6 个 `sharp_linemem` 级联（[L29-36](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36)）：每个延迟 **1 行**（`ram_array` 容量 1280，见 [sharp_linemem.vhd:20](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_linemem.vhd#L20)，正好是 720p 一行的有效像素数）。这是**主导延迟**。
   - 垂直 `sharp_arith`（[L39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49)）：进程内有 `wait until rising_edge(clk)`，**1 级寄存器**输出（见 [sharp_arith.vhd:29-45](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L29-L45)）。
   - 水平抽头进程 `h_tap(0) <= v_out;` + 移位（[L51-58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58)）：进入 **1 级寄存器**，之后中心抽头 `h_tap(3)` 还要再等约 3 拍。
   - 水平 `sharp_arith`（[L60-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70)）：**1 级寄存器**，输出 `data_out = r_1`。
3. **终点（模块③）**：回到 [sharp.vhd:108](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L108) `r_out <= std_logic_vector(to_unsigned(r_1,8));`——最后 1 级寄存器（兼做反向类型转换）。

### 需要观察的现象 / 预期结果

把链路整理成一张表（“纯流水线寄存器”指按时钟周期计的触发器，“行级延迟”指行存储造成的、以一整行为单位的延迟）：

| 级 | 位置 | 类型 | 延迟量级 |
| --- | --- | --- | --- |
| 1 | 输入寄存器 `r_0`（sharp.vhd:59） | 触发器 | 1 个时钟周期 |
| 2 | `v_tap(0)`（sharp_slice.vhd:27） | 连线 | 0 |
| 3 | 6× `sharp_linemem` 链 | 行存储 | 每个延迟 1 行（1280 有效像素）；中心抽头 `v_tap(3)` 相对输入约延迟 3 行 |
| 4 | 垂直 `sharp_arith` | 触发器 | 1 个时钟周期 |
| 5 | 水平抽头 `h_tap(0)` + 移位 | 触发器链 | 进入 1 拍；中心 `h_tap(3)` 再等约 3 拍 |
| 6 | 水平 `sharp_arith` | 触发器 | 1 个时钟周期 |
| 7 | 输出寄存器 `r_out`（sharp.vhd:108） | 触发器 | 1 个时钟周期 |

**关键结论**：

- 纯流水线寄存器（第 1、4、5 进入、6、7 级）合计约 5–6 个时钟周期（不含水平中心抽头等待的 ~3 拍）。
- 真正的大头是**行存储**（第 3 级）：要让垂直滤波的中心抽头对齐，必须等约 3 整行像素——这是任何“逐行扫描 + 行间卷积”视频滤波器都躲不开的代价，也是本设计占用片上 RAM 的主因。
- 同步信号由 `sharp_control(delay=6)` 延迟 6 拍来与数据粗略对齐；**注意**：行存储造成的“行级”偏移，实际上靠的是 `de` 驱动的写使能与逐行扫描的天然结构来消化，而 `delay=6` 主要补偿流水线寄存器那几拍。精确的对齐分析（为什么恰好是 6）是 u3-l2 的主题。

> 待本地验证：上表中的“约 3 行”“约 3 拍”“5–6 个时钟周期”是基于源码静态推断的；由于行存储的地址推进受 `de` 门控影响，精确到一个像素的总延迟周期数建议用 u5 的仿真 testbench 实测确认。

### 如果想进一步动手

在**自己的副本**上（不要改动课程仓库的源码）做下面任一小实验，并用 u5 的自校验 testbench 观察：

- 把 `sharp_control` 的 `delay` 从 6 改成 3 或 9，看输出图像是否整体平移、mismatch 是否增加；
- 临时去掉一级 `sharp_arith` 的寄存器（仅作为理解延迟的练习），观察时序与结果的变化。

## 6. 本讲小结

- 顶层 `sharp.vhd` 是**边界适配器**：输入寄存器把外部 `std_logic_vector` 转成内部 `integer` 并打一拍，输出寄存器再反向转回 `std_logic_vector`。
- 复位在边界做了极性翻转：`reset <= not reset_n`，让内部所有子模块统一使用高有效复位。
- R/G/B 三个颜色分量各例化一个 `sharp_slice`，是“**用面积换吞吐**”的取舍；三个实例共享 `clk/reset/de_0`，数据各自独立。
- 视频同步信号 `vs/hs/de` 由 `sharp_control(delay=6)` 延迟固定拍数，使其与数据通路延迟对齐；输出端同步信号与像素在**同一个进程**里一起打一拍，保证同拍到达。
- `enable` 在本版本是“采样但未使用”的预留信号；`clk_o` 直通时钟、`led` 恒灭，输出消隐期不强制清零 RGB（依赖 `de_out` 标记有效性）。
- 一个像素从 `r_in` 到 `r_out` 要经过若干级流水线触发器（约 5–6 拍）以及行存储带来的**行级延迟**（主导），后者是视频行间卷积的根本代价。

## 7. 下一步学习建议

- **u3-l2 视频时序与 sharp_control 同步延迟**：本讲把 `delay=6` 当成给定值，下一讲会解释为什么是 6——把数据通路延迟和同步延迟的对齐关系讲透。
- **u3-l3 sharp_slice 的二维滤波数据流**：本讲把 `sharp_slice` 当黑盒，下一讲打开它，精读 6 个 `sharp_linemem` 如何级联出 7 个垂直抽头、水平移位如何出 7 个水平抽头、两次 `sharp_arith` 如何串成可分离二维滤波。
- **U4 核心模块**：`sharp_linemem` 的循环缓冲（u4-l1）与 `sharp_arith` 的定点乘加/饱和截断（u4-l2）的逐行精读。
- 阅读建议：先把本讲的“像素链路表”和 u3-l3 的“抽头数据流图”对照看，会对整个数据通路有完整的画面感。
