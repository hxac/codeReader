# 绘图原语：线与几何形状

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「硬件绘图」和「软件绘图」的根本差异：硬件是**流式地输出像素地址**，由帧缓冲/行缓冲消费。
- 看懂 `draw_line.sv` 如何用 **Bresenham 算法**只用加减法（无乘除法）画出任意方向的直线。
- 理解 `draw_rectangle_fill.sv` 如何通过「逐行调用一条水平线」把填充矩形拆解成更简单的原语。
- 理解 `draw_circle.sv` 如何用 **Zingl 误差递推**画出圆轮廓，并把它的 `err` 项和隐式圆方程 \(x^2+y^2-r^2\) 对应起来。
- 学会阅读这套模块统一的 `start/oe/drawing/busy/done` 接口，并能据此在仿真器里手算像素序列。

## 2. 前置知识

### 2.1 为什么 FPGA 上画线不能用「软件那一套」

在 CPU 上画一条线，你写一个 `for` 循环，对每个 x 算出 `y = round(k*x + b)`，再把像素写进显存。这背后隐藏了两个 FPGA 上很奢侈的东西：

- **乘法**：算 `k*x` 要乘法器。
- **随机写显存**：CPU 有大显存和地址翻译，FPGA 的片上 RAM 宝贵且按行/块组织。

FPGA 的画法是另一种范式：**专门设计一个硬件状态机，它一拍一拍地把「下一个该点亮的像素坐标」吐出来**，再由帧缓冲把颜色写进去。于是：

- 坐标计算被摊到每个时钟周期，用增量（加法）更新，而不是每次重算。
- 整个画线过程是一个**有限状态机**，有时序（开始、忙、完成），可以暂停（`oe`）。

这就是本讲的中心思想：**绘制 = 流式产生像素坐标，命中 = 写入颜色**。

### 2.2 你需要复习的两个数学事实

- 一个整数的平方可以递推：\((n+1)^2 = n^2 + 2n + 1\)。所以「平方」不必做乘法，只需维护一个误差项，每次加 \(2n+1\)。这正是 Bresenham 与 Zingl 算法节省乘法的关键。
- 圆的隐式方程：\(x^2 + y^2 = r^2\)。圆内的点满足 \(x^2 + y^2 \le r^2\)，圆上的点近似满足 \(x^2 + y^2 \approx r^2\)。本讲的 `draw_circle` 不直接做这个比较（那太贵），而是维护一个由它导出的误差项——二者数学上是等价的。

### 2.3 衔接上一讲

u6-l1 讲了显示时序模块 `display_480p` 等，它们每拍输出一个带符号扫描坐标 `(sx, sy)` 和有效区信号 `de`。本讲的绘图原语**不直接驱动显示器**，而是把要画的形状转换成一串 `(x, y)` 坐标，供帧缓冲（u6-l4）写入；显示时再由 `display` 模块把帧缓冲读出来上屏。所以本讲是「几何 → 坐标流」，上一讲是「坐标流 → 屏幕像素」，两者通过存储器衔接。

## 3. 本讲源码地图

本讲全部源码位于 `ThreePart/projf-explore/lib/graphics/`（projf 库的 graphics 分区，MIT 许可）。

| 文件 | 作用 | 是否本讲精读 |
|---|---|---|
| `README.md` | graphics 分区总览与**统一接口约定** | 是（接口部分） |
| `draw_line.sv` | 任意方向直线（Bresenham） | 是（核心） |
| `draw_line_1d.sv` | 单向水平线（假设 x1≥x0），被矩形/圆/三角形复用 | 是（辅助） |
| `draw_rectangle_fill.sv` | 填充矩形 | 是（核心） |
| `draw_circle.sv` | 圆轮廓（Zingl 算法） | 是（核心） |
| `draw_circle_fill.sv` | 填充圆（复用 Zingl + 水平线） | 略读 |
| `draw_triangle_fill.sv` | 填充三角形（双边扫掠 + 水平线） | 是（进阶） |
| `xc7/draw_line_tb.sv`、`xc7/draw_circle_tb.sv` | Vivado/iverilog 仿真用的 testbench | 实践用 |

> 说明：`draw_rectangle.sv`（空心矩形）画 4 条边，`draw_triangle_fill.sv` 画填充三角形，它们都建立在 `draw_line` 之上。本讲以「直线 → 填充矩形 → 圆 → 填充三角形」的顺序，展示 projf 库「**复杂形状由简单原语组合而成**」的分层思想。

## 4. 核心概念与源码讲解

### 4.1 统一接口与「流式输出像素地址」思想

#### 4.1.1 概念说明

projf 的所有绘图模块共享**同一套接口**，这意味着你可以用同样的方式驱动线、矩形、圆、三角形——上层控制器代码几乎不变。理解这套接口是阅读任何一个绘图模块的前提。

#### 4.1.2 核心流程

一次绘制的生命周期：

1. **空闲（IDLE）**：模块等 `start` 信号。
2. **启动**：拉高 `start` 一拍，模块锁存顶点坐标、置 `busy=1`。
3. **绘制（DRAW）**：每个时钟周期，只要 `oe=1`，模块在 `x,y` 上输出「当前该画的像素」，并拉高 `drawing`。`oe=0` 时暂停（不前进、不输出），用于和帧缓冲的写端口节拍对齐。
4. **完成**：画完最后一个像素后，`done` 拉高一拍，`busy` 归零，回到 IDLE。

关键点：`drawing` 高电平时，**当前 `(x,y)` 就是一个属于该形状的像素**，消费端（帧缓冲）此刻应当把颜色写到这个地址。

#### 4.1.3 源码精读

接口约定的权威来源是 graphics 分区的 README：

[README.md:29-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/README.md#L29-L46) —— 列出了 `clk/rst/start/oe/(x0,y0)/(x1,y1)/(x2,y2)/r0/(x,y)/drawing/busy/done` 这一整套共享信号，并说明三角形用第三个顶点 `(x2,y2)`、圆用半径 `r0`。

关于坐标宽度：

[README.md:47-48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/README.md#L47-L48) —— 坐标是**有符号**的，位宽由参数 `CORDW` 决定，默认 16 位（范围 −32768~+32767）。有符号是为了配合 u6-l1 里 `display` 模块用负坐标表示消隐区的约定。

#### 4.1.4 代码实践

**实践目标**：先不动手画图，而是把「一次绘制」看成一次握手机制。

**操作步骤**：

1. 打开 `draw_line.sv`，找到模块端口（见 4.2.3 的链接）。
2. 对照 README 接口表，逐个标注 `draw_line` 的端口属于「控制类」（`start/oe/busy/done`）还是「数据类」（`x0,y0,x1,y1,x,y`）。
3. 画出时序示意：`start` 一拍 → `busy` 拉高 → 若干拍 `drawing` 脉冲 → `done` 一拍 → `busy` 拉低。

**需要观察的现象**：你会注意到 `start` 只需一拍（testbench 里都是 `start=1; #10 start=0;`），而 `oe` 是电平（持续为 1 才持续画）。

**预期结果**：控制信号 `start/done` 是脉冲，`oe/busy/drawing` 是电平。

#### 4.1.5 小练习与答案

**练习**：为什么需要 `oe`（输出使能）？如果删掉它、让模块全速画，会出什么问题？

**参考答案**：因为绘图模块和显示模块常常**共享同一个帧缓冲存储器**。显示要在每行消隐期之外持续读帧缓冲，绘图要写帧缓冲；双口 RAM 也只能在一拍内服务有限的读写。`oe=0` 让绘图模块暂停输出，把写口让给更高优先级的访问，从而避免读写冲突。没有 `oe`，绘图会强占写端口，显示可能出现撕裂或丢像素。

---

### 4.2 draw_line：Bresenham 无乘法直线

#### 4.2.1 概念说明

画一条从 \((x_0,y_0)\) 到 \((x_1,y_1)\) 的直线，朴素做法是每个 x 算 \(y = kx+b\)（要乘法、要浮点）。**Bresenham 算法**（1965）的洞见是：与其算出「真实 y 值」再取整，不如维护一个**整数误差项 `err`**，记录当前像素偏离真实直线的累积量，每步只需比较 `err` 和阈值、用加减法更新。整个过程**没有一次乘除法**，非常适合硬件。

projf 的实现参考自 Alois Zingl 的《The Beauty of Bresenham's Algorithm》，处理了所有八方向（左/右、上/下、陡/缓），并能画水平线、垂直线、单点。

#### 4.2.2 核心流程

`draw_line` 的策略是**归一化方向**后只走「从上往下、可能左可能右」的统一逻辑：

1. **交换顶点**：若 \(y_0 > y_1\)，交换两端点，保证永远从 y 较小的一端（上方）开始，**y 只增不减**。x 方向另存一个 `right` 标志（左→右或右→左）。
2. **初始化**（INIT_0/INIT_1 两拍）：计算
   - \(dx = |x_b - x_a|\)（水平跨度，非负）
   - \(dy = y_a - y_b = -|y_b - y_a|\)（注意取负，因为 y 向下增长）
   - 误差初值 \(err = dx + dy\)
3. **绘制（DRAW）**：每拍判断两个方向是否要移动：
   - `movx = (2*err >= dy)`：水平方向该不该走一步？
   - `movy = (2*err <= dx)`：垂直方向该不该走一步？（注意 y 永远 +1）
   - 两个都真时走斜对角，并按 `err + dy + dx` 更新；只走一个时按对应分量更新。

误差更新的本质是把浮点斜率 \"累积\" 成整数：每当只走水平（没向下跟），误差就 \"欠\" 一点（加 dy，dy 为负）；每当向下走，误差就 \"还\" 一点（加 dx）。误差在 0 附近来回振荡，直线就被 \"最佳逼近\"。

#### 4.2.3 源码精读

模块端口与方向归一化的组合逻辑：

[draw_line.sv:24-36](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_line.sv#L24-L36) —— `swap = (y0 > y1)` 决定是否把两点交换，使 `ya <= yb`（上端点记为 `xa,ya`，下端点记为 `xb,yb`）。这样 DRAW 里 `y` 只需 `+1`。

误差与移动判断：

[draw_line.sv:38-45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_line.sv#L38-L45) —— 注意 `err/dx/dy` 比 `CORDW` 宽 1 位（`signed [CORDW:0]`），防止有符号运算溢出。`drawing` 用 `always_comb` 组合输出 = `(state==DRAW && oe)`。

DRAW 状态的核心三段（水平走 / 垂直走 / 斜对角）：

[draw_line.sv:53-75](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_line.sv#L53-L75) —— 由于 Verilog **非阻塞赋值**「后写覆盖先写」，当 `movx && movy` 同时成立时，第三个 `if` 块的赋值生效（x、y、err 同时更新）。终止条件是 `x==x_end && y==y_end`。

初始化两拍与空闲启动：

[draw_line.sv:76-96](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_line.sv#L76-L96) —— INIT_0 算 `dx/dy`，INIT_1 算 `err` 初值并装载起点；IDLE 里 `right <= (xa < xb)` 决定 x 方向。`dx/dy/err` 拆成两拍是为了让关键路径更短（利于高频）。

#### 4.2.4 代码实践：手算 (0,0)→(5,3) 的像素序列

**实践目标**：用纸笔跟踪 `draw_line` 的状态机，验证它画出的直线和直觉一致。

**操作步骤**：

1. 输入 `x0=0,y0=0,x1=5,y1=3`。
2. 方向归一化：`swap=(0>3)=0`，故 `xa=0,xb=5,ya=0,yb=3`；`right=(0<5)=1`。
3. INIT_0：`dx = xb-xa = 5`；`dy = ya-yb = -3`。
4. INIT_1：`err = dx+dy = 2`；起点 `(x,y)=(0,0)`，终点 `(x_end,y_end)=(5,3)`。
5. 逐拍跟踪 DRAW（`movx=(2*err>=dy)`，`movy=(2*err<=dx)`，`oe=1`）：

| 拍 | 本拍绘制 (x,y) | 进入时 err | movx (2err≥−3) | movy (2err≤5) | 本拍结束后 |
|---|---|---|---|---|---|
| 1 | (0,0) | 2 | T (4≥−3) | T (4≤5) | x=1,y=1,err=2−3+5=**4** |
| 2 | (1,1) | 4 | T (8≥−3) | F (8≤5 假) | x=2,err=4−3=**1** |
| 3 | (2,1) | 1 | T (2≥−3) | T (2≤5) | x=3,y=2,err=1−3+5=**3** |
| 4 | (3,2) | 3 | T (6≥−3) | F (6≤5 假) | x=4,err=3−3=**0** |
| 5 | (4,2) | 0 | T (0≥−3) | T (0≤5) | x=5,y=3,err=0−3+5=**2** |
| 6 | (5,3) | — | — | — | 命中 `x==x_end && y==y_end` → done |

**需要观察的现象**：第 6 拍 `drawing` 仍为高（state 还是 DRAW），所以 (5,3) 也会被画出；之后状态才转 IDLE。

**预期结果**：6 个像素 **(0,0)→(1,1)→(2,1)→(3,2)→(4,2)→(5,3)**。

**对照验证**：理想直线斜率 \(k=3/5=0.6\)，对 x=0..5 取 round(0.6x) 得 y=0,1,1,2,2,3，与上面逐拍结果**完全一致**。

> 待本地验证：上述为依据源码手工推导。你可用 iverilog 跑 `xc7/draw_line_tb.sv`（它用 `CORDW=9`）对照——把 testbench 里 case 改成 `(0,0)→(5,3)` 或直接读 `$monitor` 打印验证逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `err/dx/dy` 要比坐标宽 1 位（`signed [CORDW:0]`）？

**参考答案**：两个 CORDW 位有符号数相减/相加可能产生 CORDW+1 位的结果（例如两个 16 位数之差需要 17 位才不溢出）。多留 1 位符号位保证中间运算不溢出，是定点/整数运算的常见防御。

**练习 2**：水平线（`y0==y1`）和垂直线（`x0==x1`）会不会出错？

**参考答案**：不会。水平线：`dy=0`，`movy=(2*err<=dx)` 在 `err=dx>0` 时不成立，只走 x；垂直线：`dx=0`，`movx=(2*err>=dy)` 不成立，只走 y。两种退化情形都被同一套逻辑自然覆盖，这正是 Bresenham 的鲁棒之处。

---

### 4.3 draw_rectangle_fill：填充矩形 = 逐行水平线

#### 4.3.1 概念说明

填充矩形是\"最简单\"的二维形状：它在每一行 y 上，从左边界画一条水平线到右边界。projf 的实现把\"画一条水平线\"这件事**复用一个更简单的子模块 `draw_line_1d`**（1D，单向，假设 x1≥x0），自己只负责\"从上到下逐行递进\"。

这是一个关键的工程思想：**不要为每个形状从头写状态机，而是把形状拆成更简单的原语组合**。矩形 = 多条水平线；后面你会看到，圆和三角形也复用 `draw_line_1d`。

#### 4.3.2 核心流程

1. **排序 y**：组合逻辑把 `y0,y1` 排成 `y0s <= y1s`（上小下大），保证从上画到下。
2. **排序 x**：INIT 时把 `x0,x1` 排成 `lx0 <= lx1`，作为每行水平线的左右端点（整个矩形期间 x 端点不变）。
3. **逐行**：用 `line_id` 记录当前是第几行，`y = y0s + line_id`。每行启动一次 `draw_line_1d`（`line_start` 脉冲），等它 `done`（`line_done`）后 `line_id++`，进入下一行的 INIT。
4. **终止**：当某行 `y == y1s`（最后一行）且该行画完，整个矩形完成。

#### 4.3.3 源码精读

顶层状态机（INIT/DRAW/IDLE 三态）：

[draw_rectangle_fill.sv:36-69](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_rectangle_fill.sv#L36-L69) —— INIT 里 `lx0/lx1` 只算一次（x 端点对整个矩形不变），`y <= y0s + line_id` 决定当前行；DRAW 里检测 `line_done`，未到最后一行就回 INIT 并 `line_id <= line_id + 1`。

例化 `draw_line_1d` 画水平线：

[draw_rectangle_fill.sv:80-93](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_rectangle_fill.sv#L80-L93) —— 把 `(lx0,lx1)` 作为水平线端点，`line_start` 作启动，`line_done` 接子模块的 `done`。注意 `busy` 端口悬空（`/* PINCONNECTEMPTY */`），只取 `done`。

被复用的 `draw_line_1d` 长什么样：

[draw_line_1d.sv:21-53](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_line_1d.sv#L21-L53) —— 它只有 IDLE/DRAW 两态，`x` 从 `x0` 每拍 +1 直到 `x1`，是\"最朴素\"的扫描器。因为它假设 `x1>=x0`，所以不需要 Bresenham 的误差逻辑，面积更小、速度更快——这就是矩形/圆/三角形都爱复用它的原因。

#### 4.3.4 代码实践：数清一个 4×3 矩形要画多少像素

**实践目标**：建立\"填充矩形像素数 = 高 × 宽\"的直觉，并验证 `line_id` 与行数的对应。

**操作步骤**：

1. 设矩形顶点 `(x0,y0)=(1,1)`，`(x1,y1)=(4,3)`（左上、右下）。
2. 排序后 `y0s=1,y1s=3`，共 \(3-1+1=3\) 行（y=1,2,3）。
3. 每行水平线 `lx0=1,lx1=4`，共 \(4-1+1=4\) 个像素（x=1,2,3,4）。
4. 总像素 = 3 行 × 4 列 = **12 个**。

**需要观察的现象**：`line_id` 从 0 递增到 2（共 3 行）；最后一行 `y==y1s==3` 时画完即 `done`。

**预期结果**：12 个像素依次输出，覆盖坐标 \(\{(x,y)\mid 1\le x\le 4,\ 1\le y\le 3\}\)。

> 待本地验证：可在 `xc7/draw_rectangle_fill_tb.sv` 上跑 iverilog 观察 `$monitor` 输出的 (x,y) 序列是否符合上述范围。

#### 4.3.5 小练习与答案

**练习**：如果用户传进来的 `x0 > x1`（右上是第一个顶点），矩形会画错吗？

**参考答案**：不会。INIT 里有 `lx0 <= (x0>x1)?x1:x0; lx1 <= (x0>x1)?x0:x1;`，自动把较小的 x 作为左端点、较大的作为右端点。y 方向同理（`y0s/y1s` 组合排序）。所以无论顶点顺序，模块都能正确画出同一个填充矩形。这种\"输入顺序无关\"的设计降低了上层使用的心智负担。

---

### 4.4 draw_circle：Bresenham 圆与误差递推

#### 4.4.1 概念说明

画圆轮廓的朴素想法是：对每个像素判断 \(x^2+y^2 \approx r^2\) 是否成立（填充圆则是 \(x^2+y^2 \le r^2\)）。但这要么遍历整屏像素、要么每步做平方运算，对硬件都不友好。

Zingl 的圆算法把\"到圆心的距离误差\"维护成一个**整数误差项 `err`**，每步只比较 `err` 与阈值、用加减法更新，且**只算一个八分圆（octant）**，再通过对称镜像到 4 个象限——圆有 8 路对称性，算 1/8 就够了。projf 的 `draw_circle` 正是这一算法。

> ⚠️ **诚实说明**：本讲的实践任务提到\"\(x^2+y^2\le r^2\) 比较\"，但 `draw_circle.sv` **并不直接做这个比较**。它用的是 Zingl 误差递推。二者数学等价：`err` 就是由隐式圆方程 \(f(x,y)=x^2+y^2-r^2\) 导出的离散误差。下面我们会把这个对应关系讲清楚。

#### 4.4.2 核心流程

算法从圆的**最左点**（相对圆心 \((-r,0)\)）出发，沿圆弧走到**最上点** \((0,-r)\)（注意 projf 里 y 向下为正，代码用 \((xa,ya)\) 表示相对坐标，\(xa\) 从 \(-r\) 走向 0）。每得到一个八分圆点 \((xa,ya)\)，立刻镜像出 4 个象限的 4 个绝对坐标。

误差更新规则（来自 \((n+1)^2=n^2+2n+1\)）：

- 若 `err <= ya`：y 方向\"多走了一步\"更接近圆，故 \(ya \leftarrow ya+1\)，并 \(err \leftarrow err + 2(ya+1)+1\)。
- 若 `err_tmp > xa` 或 `err > ya`：x 方向该收一步，故 \(xa \leftarrow xa+1\)（向 0 靠拢），并 \(err \leftarrow err + 2(xa+1)+1\)。

这里 \(2n+1\) 正是平方差 \((n+1)^2-n^2\)。所以 `err` 本质上是 \(x^2+y^2-r^2\) 的增量形式——**这就是它与 \(x^2+y^2\le r^2\) 的联系**：直接比较要每次算平方，增量法把平方差预算成 \(2n+1\)，于是只剩加减法和比较。

#### 4.4.3 源码精读

IDLE 启动与初值装载：

[draw_circle.sv:84-94](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_circle.sv#L84-L94) —— 起点 `xa=-r0, ya=0`（最左点），`err = 2 - 2*r0`（Zingl 标准初值），`quadrant=0`。

CALC_Y：决定是否在 y 方向走一步：

[draw_circle.sv:35-50](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_circle.sv#L35-L50) —— 先存 `err_tmp <= err`（保存\"y 步之前\"的误差，供 CALC_X 用），再判断 `err <= ya`。注意终止条件 `xa==0`（走完八分圆）。

CALC_X：决定是否在 x 方向收一步：

[draw_circle.sv:51-59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_circle.sv#L51-L59) —— 条件 `err_tmp > xa || err > ya`，其中 `err_tmp` 是 y 步前的误差、`err` 是 y 步后的误差，这正是 Zingl 原始 C 代码里 `r > x || err > y` 的硬件化。

DRAW：把一个八分圆点镜像成 4 个象限的绝对坐标：

[draw_circle.sv:60-83](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_circle.sv#L60-L83) —— 4 个象限分别用 `(x0±xa, y0±ya)` 组合产出 4 个点；`quadrant` 每拍 +1，第 4 个象限（2'b11）画完后回到 CALC_Y 继续算下一个八分圆点。注意 `err` 位宽是 `CORDW+2`（4 倍宽余量），因为 `2*(xa+1)+1` 这类运算会放大位宽。

#### 4.4.4 代码实践：手算圆心 (0,0)、半径 4 的轮廓点

**实践目标**：跟踪 `draw_circle` 的 `xa,ya,err`，验证它画出一个半径为 4 的圆，并理解 `err` 与 \(x^2+y^2\) 的关系。

**操作步骤**：

1. IDLE 启动：`xa=-4, ya=0, err = 2-2*4 = -6`，`quadrant=0`。
2. 第一个 DRAW 直接画（xa=-4,ya=0 的 4 象限镜像）：得 (4,0)、(−4,0)（y=0 时上下象限重合）。
3. 之后每轮 CALC_Y → CALC_X → DRAW(×4 象限)。逐轮跟踪相对坐标 \((xa,ya)\)：

| 轮 | DRAW 时的 (xa,ya) | 进入 CALC_Y 的 err | y 步后 err | x 步? | 4 象限绝对坐标（圆心 0,0） |
|---|---|---|---|---|---|
| 0 | (−4,0) | −6 | −3 | 否 | (4,0),(−4,0),(−4,0),(4,0) |
| 1 | (−4,1) | −3 | 2 | 是→xa=−3, err=−3 | (4,1),(−4,1),(4,−1),(−4,−1) |
| 2 | (−3,2) | −3 | 4 | 是→xa=−2, err=1 | (3,2),(−3,2),(3,−2),(−3,−2) |
| 3 | (−2,3) | 1 | 10 | 是→xa=−1, err=9 | (2,3),(−2,3),(2,−3),(−2,−3) |
| 4 | (−1,4) | 9 | 9（y 不步） | 是→xa=0, err=10 | (1,4),(−1,4),(1,−4),(−1,−4) |
| 5 | (0,4) | — | — | — xa==0 → done | (0,4),(0,4),(0,−4),(0,−4) |

**需要观察的现象**：八分圆点 \((xa,ya)\) 从 (−4,0) 走到 (0,4)，共 6 个点；镜像后覆盖整个圆周；最大 \(|y|=4\)，恰好等于半径，没有越界。

**预期结果（去重后的轮廓像素）**：

\[
\{(\pm4,0),\ (\pm4,\pm1),\ (\pm3,\pm2),\ (\pm2,\pm3),\ (\pm1,\pm4),\ (0,\pm4)\}
\]

**对照 \(x^2+y^2\)**：抽查 (3,2) 得 \(9+4=13\)，(1,4) 得 \(1+16=17\)，都在 \(r^2=16\) 附近振荡——这正是\"误差项 `err` ≈ \(x^2+y^2-r^2\)\"的体现。直接用 \(x^2+y^2\le r^2\) 会得到**填充**圆（如 `draw_circle_fill.sv`）；`draw_circle` 只取\"最接近圆周\"的那一圈像素，故是**轮廓**。

> 待本地验证：上述为依据源码手工推导。`xc7/draw_circle_tb.sv` 的 case 0 正是\"圆心 (0,0) 半径 4\"，可用 iverilog 跑出 `$monitor` 序列对照。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `err` 位宽是 `CORDW+2`，而直线里只需 `CORDW+1`？

**参考答案**：圆的误差更新含 `2*(xa+1)+1`、初始 `2-2*r0` 等，量级约为半径的 2 倍，且 `xa` 可为负（\(-r\)），运算中符号扩展需要更多位。多 2 位保证 `2*n` 这类乘 2 运算不溢出。

**练习 2**：如果想要**填充**圆，该改哪里？

**参考答案**：不必改 `draw_circle`。projf 单独提供了 `draw_circle_fill.sv`，它复用同样的 Zingl 误差递推算出每个八分圆点 \((xa,ya)\)，但每轮不再只镜像 4 个轮廓点，而是用 `draw_line_1d` 在 \(y=y_0\pm ya\) 两行各画一条从 \(x_0-|xa|\) 到 \(x_0+|xa|\) 的水平线（见 `draw_circle_fill.sv` 的 CORDS_DOWN/LINE_DOWN/CORDS_UP/LINE_UP 状态）。于是逐行水平填充，得到实心圆。

---

### 4.5 draw_triangle_fill：双边扫掠（进阶，展示组合的力量）

#### 4.5.1 概念说明

填充三角形比矩形难：它的左右边界是两条斜边。projf 的思路非常巧妙——**同时跟踪两条边在当前 y 上的 x 交点，再在两个交点之间画一条水平线**。具体来说，三角形被分成\"上边\"和\"下边\"两段（按中间顶点 y 排序），一条边（A 边）从顶到底贯穿，另一条边（B 边）分上下两段；每到一个新的 y，用两条 `draw_line` 算出左右交点，再用 `draw_line_1d` 填中间。

这是\"复杂形状 = 简单原语组合\"思想的集大成者：三角形 = 2 个 `draw_line`（算左右边交点）+ 1 个 `draw_line_1d`（填水平线）。

#### 4.5.2 核心流程

1. **排序三个顶点**（SORT_0/1/2 三拍）：按 y 从小到大排成 \(y_{0s}\le y_{1s}\le y_{2s}\)，得到上、中、下三个顶点。
2. **准备两条边**：A 边恒为\"上顶点→下顶点\"；B 边分两段——上段\"上顶点→中顶点\"，下段\"中顶点→下顶点\"（由 `b_edge` 切换）。
3. **逐行扫掠**（EDGE）：让 A、B 两条 `draw_line` 都向前走到当前 y，取出它们各自最新的 x 交点 `xa/xb`。
4. **填水平线**（H_LINE）：在 `min(xa,xb)` 到 `max(xa,xb)` 之间用 `draw_line_1d` 画一条水平线。
5. 重复 3-4 直到 A 边走完且 B 边也走完两段。

#### 4.5.3 源码精读

13 态状态机（排序 + 初始化 + 扫掠 + 填充 + 完成）：

[draw_triangle_fill.sv:47-48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_triangle_fill.sv#L47-L48) —— `enum {IDLE, SORT_0, SORT_1, SORT_2, INIT_A, INIT_B0, INIT_B1, INIT_H, START_A, START_B, EDGE, H_LINE, DONE}`，是本讲最复杂的状态机。排序用 \"比较-交换\" 网络（SORT_0 比 0/2，SORT_1 比 0/1，SORT_2 比 1/2）把三个顶点按 y 排好。

EDGE：等两条边都走到当前 y：

[draw_triangle_fill.sv:115-121](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_triangle_fill.sv#L115-L121) —— 条件 `(ya != prev_y || !busy_a) && (yb != prev_y || !busy_b)` 确保 A、B 两边都已推进到新的一行，再取左右交点（自动按大小排成左→右）。

三个被例化的原语：

[draw_triangle_fill.sv:170-219](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/graphics/draw_triangle_fill.sv#L170-L219) —— `draw_edge_a`（A 边）、`draw_edge_b`（B 边）都是 `draw_line`；`draw_h_line` 是 `draw_line_1d`。注意 `oe_a/oe_b` 的组合逻辑（L155-159）只在\"边的 y 正好等于当前行\"时允许该边输出，巧妙地让两条边按行对齐。

#### 4.5.4 代码实践：阅读型——画一个直角三角形

**实践目标**：不手算全部像素，而是验证\"三角形 = 两边交点 + 水平线\"的策略正确性。

**操作步骤**：

1. 取三角形顶点 \((0,0),(4,0),(0,3)\)（直角在原点）。
2. 排序后（按 y）：上 (0,0)、中 (4,0) 或 (0,3)、下 (0,3)——注意 (0,0) 与 (4,0) 同 y。
3. 想象 A 边从上顶点走到下顶点、B 边分段。对每一行 y，A、B 边各给一个 x 交点，水平线连接二者。

**需要观察的现象**：底边（y=0）最宽（从 x=0 到 x=4），顶点处（y=3）收窄到一个点。这正是\"逐行水平填充\"的视觉效果。

**预期结果**：填充区域是 \(x/4 + y/3 \le 1\) 的整数点集合（第一象限内的直角三角形）。

> 待本地验证：可在 `xc7/draw_triangle_fill_tb.sv` 上跑 iverilog，观察 `$monitor` 输出的 (x,y) 是否构成上述三角形。

#### 4.5.5 小练习与答案

**练习**：为什么三角形需要 `prev_xa/prev_xb/prev_y` 这些\"上一拍\"寄存器？

**参考答案**：因为两条 `draw_line` 边是\"流式\"推进的，它们的当前 x 交点要等边走到对应 y 才有效。模块需要在水平线画完后再\"安全地\"把当前交点存为\"上一行的交点\"（H_LINE 状态里 `prev_xa <= xa; prev_xb <= xb; prev_y <= yb;`），用于判断\"是否进入了新的一行\"（EDGE 里 `ya != prev_y`）。这是把两个异步推进的流\"按行同步\"起来的标准手法。

---

## 5. 综合实践：用三个原语拼一幅「日落」小图

把本讲的知识串起来：用 `draw_rectangle_fill`（天空）、`draw_circle_fill` 或 `draw_circle`（太阳）、`draw_line`（地平线/海平面）拼一幅极简画面，并用仿真打印验证坐标。

**任务**：

1. 设画面坐标系（仿照 u6-l1，比如 0~63 的正坐标区域）。
2. 设计一个顶层 `top_sunset`，**按顺序**依次驱动三个绘图模块（注意它们共享 `(x,y,drawing)` 输出，需用一个简单仲裁：上一个 `done` 后才 `start` 下一个）：
   - 先 `draw_rectangle_fill` 画天空（例如 (0,0)~(63,31)）。
   - 再 `draw_circle_fill` 画太阳（圆心 (48,10)，半径 6）。
   - 最后 `draw_line` 画海平面（(0,32)~(63,32)）。
3. 在每个模块 `drawing` 为高时，用 `$write` 把 `(x,y)` 打印出来（或写入一个 `.csv`）。
4. 用这些坐标在方格纸上手绘，确认画面合理：上方矩形、右上角圆、中间一条横线。

**观察要点**：

- 三个模块是**串行**驱动的（同一时刻只画一个形状），因为它们共用输出端口和（真实场景下）帧缓冲写端口。
- 每个形状结束时 `done` 脉冲是切换到下一个形状的触发。
- 太阳若改用 `draw_circle`（轮廓）而非 `draw_circle_fill`，画面上太阳就是空心的——直观看清\"轮廓 vs 填充\"的区别。

> 待本地验证：本任务需要你编写一个顶层 testbench 并用 iverilog/Vivado 仿真。坐标范围与形状参数可自行调整。若暂时没有仿真器，至少完成\"在方格纸上按 4.2.4、4.3.4、4.4.4 的手算结果拼出这幅图\"的纸面版本。

## 6. 本讲小结

- **硬件绘图的范式**是：状态机逐拍**流式输出属于该形状的像素坐标 `(x,y)`**，配 `drawing` 脉冲，由帧缓冲消费写入颜色——与 CPU 的\"循环算 y\"完全不同。
- 所有 projf 绘图模块共享 `start/oe/(x,y)/drawing/busy/done` 接口，坐标是有符号的、位宽由 `CORDW` 参数化，便于上层用统一方式驱动。
- `draw_line` 用 **Bresenham 算法**：维护整数误差项 `err`，每步只做加减法和比较、无乘除法；通过交换顶点把任意八方向归一化为\"从上往下\"。
- 复杂形状由简单原语**组合**而成：`draw_rectangle_fill` = 逐行 `draw_line_1d`；`draw_triangle_fill` = 2 个 `draw_line`（两边交点）+ 1 个 `draw_line_1d`（水平填充）。
- `draw_circle` 用 **Zingl 误差递推**只算一个八分圆再镜像 4 象限；其 `err` 项本质是隐式圆方程 \(x^2+y^2-r^2\) 的增量形式（利用 \((n+1)^2-n^2=2n+1\) 省掉乘法），与朴素的 \(x^2+y^2\le r^2\) 比较数学等价但硬件更省。
- 这些模块**不直接驱动显示器**，而是产生坐标流供帧缓冲/行缓冲写入；与 u6-l1 的显示时序、u6-l4 的帧缓冲衔接成完整的图像流水线。

## 7. 下一步学习建议

- **下一步必读 u6-l4（帧缓冲与硬件精灵）**：那里会讲本讲产生的 `(x,y,drawing)` 坐标流如何被帧缓冲（`bram_sdp`，见 u5-l3）写入、再如何被显示模块读出上屏，把\"画\"和\"显\"连成闭环。
- 想看真实工程如何调用这些原语，可读 `ThreePart/projf-explore/graphics/framebuffers/` 下的顶层（如 `top_david_mono.sv`），观察它如何用本讲的 `draw_line/draw_rectangle_fill` 把一幅位图灌进帧缓冲。
- 对算法细节感兴趣的读者，强烈推荐 Zingl 原文《The Beauty of Bresenham's Algorithm》（`draw_line.sv`、`draw_circle.sv` 头部注释给出的链接），它统一了直线、圆、椭圆、贝塞尔的增量画法。
- 若你想动手优化，可尝试把 `draw_line` 的 INIT_0/INIT_1 两拍合并为一拍，思考这样会牺牲什么（答案：关键路径变长，最高时钟频率下降）——这是硬件设计\"时序 vs 面积\"权衡的经典练习。
