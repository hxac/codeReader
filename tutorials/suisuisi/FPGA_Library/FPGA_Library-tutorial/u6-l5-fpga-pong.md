# FPGA Pong 完整实战

> 本讲是 Unit 6（FPGA 图形与显示系统）的综合收官篇。我们把前面几讲学到的「显示时序、坐标比较、ROM」拼成一款真正能玩的游戏——Pong，并重点剖析「碰撞检测 + 状态机驱动的运动逻辑」这一在此之前从未出现的新主题。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `simple_480p` / `simple_720p` 这套「简化版显示时序」与 `lib/display` 库版本的差别，并理解它为什么更适合做游戏。
- 读懂 Pong 的六状态游戏状态机（`NEW_GAME → POSITION → READY → PLAY → POINT → END_GAME`），理解「帧节拍 `frame`」如何把 60 Hz 的游戏逻辑与 25 MHz 的像素渲染解耦。
- 看懂球的位置更新、墙壁碰撞得分、球拍反弹触发与「每 N 拍加速」的组合/时序逻辑。
- 理解玩家球拍（按键）与 AI 球拍（追球）的控制差异。
- 理解 `simple_score` 如何用一个 3×5 的字形 ROM 把分数画到屏幕角落。
- 在 Verilator+SDL 仿真或开发板上跑通 Pong，并修改球速/球拍尺寸观察行为。

## 2. 前置知识

本讲默认你已掌握前几讲的内容。如果某些概念陌生，建议先回顾：

- **u6-l1 显示时序**：行/场扫描、Active/Front Porch/Sync/Back Porch、消隐期（blanking）、`de`（data enable）、`hsync`/`vsync`。
- **u5-l3 存储器与 ROM**：`$readmemh`、同步/异步读。本讲的字形本质上是一个小型 ROM。
- **u5-l5 消抖 debounce**：机械按键需要消抖，本讲例化了 `debounce`。
- **Verilog 基础**：`always_comb` / `always_ff`、非阻塞 `<=`、`enum` 状态机、`localparam`、位宽声明。

补充两个本讲要用到的新术语：

- **AABB（Axis-Aligned Bounding Box，轴对齐包围盒）**：判断一个点是否落在一个边与坐标轴平行的矩形内的最简单方法——四个不等式。Pong 的全部碰撞检测都用它。
- **游戏节拍（game tick）**：游戏的逻辑更新（球移动、计分）不需要每个像素都算一次，每帧算一次即可。本讲用一个叫 `frame` 的、每帧只亮一个像素时钟周期的脉冲来做这件事。

## 3. 本讲源码地图

Pong 的源码全部位于 [`ThreePart/projf-explore/graphics/pong/`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/) 目录下。结构如下：

| 路径 | 作用 |
|------|------|
| `simple_480p.sv` | **简化版** 640×480@60 显示时序发生器（VGA 用） |
| `simple_720p.sv` | **简化版** 1280×720@60 显示时序发生器（DVI/HDMI 用） |
| `simple_score.sv` | 用 3×5 字形 ROM 把双方分数画到屏幕左上/右上角 |
| `xc7/top_pong.sv` | Arty 等 Xilinx 7 系 + VGA 输出的顶层（游戏核心 + VGA 引脚） |
| `xc7-dvi/top_pong.sv` | Nexys Video 等 + DVI/HDMI 输出的顶层（720p，参数放大） |
| `ice40/top_pong.sv` | iCEBreaker（Lattice iCE40）+ 外置 DVI Pmod 的顶层 |
| `sim/top_pong.sv` | Verilator 仿真用的顶层（SDL 输出，无需开发板） |
| `sim/main_pong.cpp` | Verilator C++ 顶层：驱动时钟、读键盘、把像素写进 SDL 纹理 |
| `README.md` | 各平台构建说明与按键映射 |

**一个关键事实**：四个 `top_pong.sv` 里，从「帧节拍 `frame`」到「球/球拍/碰撞/计分/上色」的整段游戏逻辑是**逐字节相同**的，差别只在三处——(1) 像素时钟从哪来（`clock_480p` / `clock_720p`）、(2) 输出是 VGA / DVI / SDL 中的哪一种、(3) 几何参数（`BALL_SIZE`、`PAD_HEIGHT` 等）是否按分辨率放大。也就是说，**游戏核心与平台/分辨率无关**。因此本讲以 VGA 版 `xc7/top_pong.sv` 为主样本精读，必要时再点出 720p 版的参数差异。

本讲按学习顺序拆成 6 个最小模块（对应规格中的 3 个最小模块：`simple_480p/720p`、`碰撞与运动逻辑`、`simple_score`）：

- 4.1 简化显示时序：`simple_480p` / `simple_720p`
- 4.2 游戏状态机：六状态与转移
- 4.3 球的运动与墙壁碰撞得分
- 4.4 命中检测与球拍反弹（含加速）
- 4.5 球拍控制：玩家与 AI
- 4.6 字形 ROM 计分：`simple_score`

---

## 4. 核心概念与源码讲解

### 4.1 简化显示时序：simple_480p / simple_720p

#### 4.1.1 概念说明

u6-l1 讲过 `lib/display/display_480p.sv` 那套「带符号坐标、有效区摆在 `sx/sy≥0`、消隐期藏进负坐标」的库版本。Pong 没有直接用它，而是自己写了一个**自包含的简化版** `simple_480p.sv` / `simple_720p.sv`。差别在于：简化版用**无符号坐标**，有效区就是 `[0, HA_END] × [0, VA_END]`，而行/场同步、前后沿都「延伸」到有效区之外的大坐标里。

为什么游戏更适合简化版？因为游戏里 `ball_x`、`padl_y` 这些坐标都是无符号的屏幕像素位置，直接拿来和 `sx`、`sy` 比较即可，不需要处理负坐标。库版本优雅但对游戏「多此一举」。

#### 4.1.2 核心流程

一行的时序由四个参数拼接（像素为单位）：

```
|<-- Active (HA_END+1) -->|<-- Front Porch -->|<-- Sync -->|<-- Back Porch -->|
0                        639                  656          752                 799(=LINE)
```

一帧同理，把「像素」换成「行」。模块做两件事：

1. **组合**：根据当前 `sx/sy` 用比较器生成 `hsync`、`vsync`、`de`。
2. **时序**：每个 `clk_pix` 上升沿让 `sx` 自增，到 `LINE` 归零并让 `sy` 自增，到 `SCREEN` 归零——即两个级联计数器。

帧率由「像素时钟频率 = 一帧总像素数 × 帧率」决定。对 480p60：

\[
f_{\text{pix}} = (\text{LINE}+1) \times (\text{SCREEN}+1) \times 60 = 800 \times 525 \times 60 = 25{,}200{,}000\ \text{Hz}
\]

对 720p60：

\[
f_{\text{pix}} = 1650 \times 750 \times 60 = 74{,}250{,}000\ \text{Hz}\ (74.25\ \text{MHz})
\]

#### 4.1.3 源码精读

先看端口与时序参数（480p 版）：[simple_480p.sv:L8-L28](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_480p.sv#L8-L28)。注意 `sx`/`sy` 是 10 位无符号（`logic [9:0]`），`HA_END=639` 表示有效像素 0~639 共 640 个；`LINE=799` 表示一行总共 800 个像素时钟。

同步与使能信号是纯组合比较（注意 480p 用了**取反 `~`**，即**负极性**同步，这是 VESA 对 480p 的规定）：[simple_480p.sv:L30-L34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_480p.sv#L30-L34)。`de` 在有效区为高、消隐期为低，后续渲染用它把消隐期强制涂黑。

坐标计数器是两级联：[simple_480p.sv:L37-L48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_480p.sv#L37-L48)。`rst_pix` 高电平异步生效（与复位同步释放的库版不同，这里简单直接清零）。

720p 版结构一模一样，只有三处不同：坐标位宽升到 12 位（`logic [11:0]`）、时序参数换成 720p 的值、同步信号改为**正极性**（不取反，这是 CTA 标准对 720p 的规定）：[simple_720p.sv:L19-L34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_720p.sv#L19-L34)。

> **对比要点**：简化版 vs 库版 `lib/display/display_480p.sv`——简化版无符号坐标、有效区在原点；库版带符号坐标、有效区在正象限、消隐期在负坐标。游戏选简化版是为了让 `ball_x` 等「屏幕像素坐标」可直接参与比较。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证你对 480p 时序参数的理解。
2. **步骤**：打开 `simple_480p.sv`，读 L19-L28 的 8 个 parameter。
3. **观察**：把 Front Porch / Sync / Back Porch 的像素数填进下表（水平方向）：

   | 区间 | 起点 | 终点 | 像素数 |
   |------|------|------|--------|
   | Active | 0 | 639 | 640 |
   | Front Porch | 640 | ? | ? |
   | Sync | ? | ? | 96 |
   | Back Porch | ? | 799 | ? |

4. **预期结果**：Front Porch 16（640→655）、Sync 96（656→751）、Back Porch 48（752→799），合计 800。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `simple_480p` 的 `sx` 用 10 位就够，而 `simple_720p` 要 12 位？
  - **A1**：480p 一行最多 800（`LINE+1`），\( \lceil\log_2 800\rceil = 10 \) 位；720p 一行 1650，\( \lceil\log_2 1650\rceil = 11 \)，但留余量用 12 位。
- **Q2**：把 `simple_480p` 的 `hsync` 表达式里的 `~` 去掉会怎样？
  - **A2**：同步极性变反，显示器若严格按 VESA 负极性识别 480p，可能无法锁存同步、黑屏或不同步。

---

### 4.2 游戏状态机：六状态与转移

#### 4.2.1 概念说明

游戏不能「一通电球就乱飞」。Pong 用一个标准的**两段式 Moore 状态机**管理整局游戏的节奏：组合逻辑算 `state_next`，时序逻辑把它寄存成 `state`。六个状态覆盖了一局游戏的完整生命周期：开局、摆球、等待发球、对打、得分暂停、终局。

#### 4.2.2 核心流程

状态转移图（箭头上的条件为真时转移，无条件表示自动转）：

```
        ┌────────────────────────────────────────────── fire(终局后) ──┐
        ▼                                                                │
   NEW_GAME ──> POSITION ──> READY ──fire──> PLAY                        │
        ▲                       ▲                  │                     │
        │                       │                  │ coll_l||coll_r      │
        │                       │                  ▼                     │
        │                     POINT <──fire────────┤ (有人得分但未胜)    │
        │                       │                  │                     │
        │                       └──────────────────┘                     │
        │                                   │ coll 且 score==WIN          │
        └───────────────────────────── END_GAME <───── fire: 停留 ────────┘
```

- `NEW_GAME`：清零双方比分。
- `POSITION`：把球摆到发球方球拍旁、重置球速与击球计数。
- `READY`：等玩家按 fire 开球。
- `PLAY`：对打，球移动、碰墙得分、碰拍反弹。
- `POINT`：有人得分但未到胜分，暂停等 fire 再发球。
- `END_GAME`：有人达到胜分（`WIN`），等 fire 开新局。

#### 4.2.3 源码精读

状态机两段式的「组合段」：[top_pong.sv:L101-L118](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L101-L118)。注意几个要点：

- `READY` 与 `POINT`、`END_GAME` 都是「等 `sig_fire`」（消抖后的 fire 按键松开脉冲，见 u5-l5）。
- `PLAY` 里检测 `coll_l || coll_r`（球碰到左右屏幕边沿＝有人得分），若任一方分数到 `WIN` 转 `END_GAME`，否则转 `POINT`。
- 最后一行 `if (!clk_pix_locked) state_next = NEW_GAME;` 是**安全兜底**：像素时钟 PLL 未锁定时强制回 `NEW_GAME`，避免在时钟不稳时乱跑游戏逻辑。

「时序段」只有一行：[top_pong.sv:L121](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L121)。

胜分阈值等游戏参数集中在文件顶部：[top_pong.sv:L22-L30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L22-L30)。`WIN=4` 表示先得 4 分胜（受 4 位计分上限 9 约束）；`SPEEDUP=5` 表示每 5 次击球球速加 1。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：验证你理解了 `PLAY` 的退出条件。
2. **步骤**：读 L109-L114 的 `PLAY` 分支。
3. **观察**：假设当前 `score_l=3`、`score_r=3`、`WIN=4`，球碰左墙使 `coll_l=1` 且 `score_r` 加到 4。
4. **预期结果**：因为 `score_r == WIN`，`state_next = END_GAME`（而不是 `POINT`）。
5. **待本地验证**：上述断言可在仿真里设断点确认。

#### 4.2.5 小练习与答案

- **Q1**：为什么 `state_next` 组合逻辑末尾要 `if (!clk_pix_locked) state_next = NEW_GAME;`？
  - **A1**：PLL 未锁定时 `clk_pix` 可能还在抖动或频率不对，此时运行游戏会出错；强制回 `NEW_GAME` 保证「时钟稳了才开始」。
- **Q2**：状态机是 Moore 型还是 Mealy 型？依据是什么？
  - **A2**：Moore 型。`state_next` 只依赖当前 `state` 和输入（`sig_fire`、`coll_*`、`score_*`），输出（游戏行为）只挂在 `state` 上，不直接把输入接到输出。

---

### 4.3 球的运动与墙壁碰撞得分

#### 4.3.1 概念说明

球是游戏里唯一「会动」的实体。它的运动逻辑要解决两个问题：**多久动一次**，以及**怎么动**。

- **多久动一次**：不能每个像素时钟都动（那球会以 25 MHz 飞过去），而是**每帧动一次**。本讲用一个叫 `frame` 的脉冲充当「游戏节拍」。
- **怎么动**：球有水平速度 `ball_spx`、垂直速度 `ball_spy`、水平方向 `ball_dx`（0=右，1=左）、垂直方向 `ball_dy`（0=下，1=上）。每帧按「方向 × 速度」更新位置；撞上下墙只翻转垂直方向；撞左右墙则判得分并置碰撞标志。

#### 4.3.2 核心流程

`frame` 脉冲的定义（每帧、在垂直消隐期起点亮一个时钟周期）：[top_pong.sv:L63-L64](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L63-L64)。

```
每帧（frame 为真的那一拍）：
  若在 PLAY：
    水平：
      若向右(dx=0):
        若 ball_x + SIZE + spx >= H_RES-1:  撞右墙 → score_l+1, coll_r=1, 球贴右墙
        否则: ball_x += spx
      若向左(dx=1):
        若 ball_x < spx:                    撞左墙 → score_r+1, coll_l=1, 球贴左墙
        否则: ball_x -= spx
    垂直：
      若向下(dy=0):
        若 ball_y + SIZE + spy >= V_RES-1:  撞下墙 → 仅翻转 dy=1（本帧不位移）
        否则: ball_y += spy
      若向上(dy=1):
        若 ball_y < spy:                    撞上墙 → 仅翻转 dy=0
        否则: ball_y -= spy
    加速：若本帧方向与上帧不同(被反弹) → shot_cnt++
          若 shot_cnt==SPEEDUP → spx+1, spy+1, shot_cnt=0
```

注意垂直撞墙的处理与水平不同：**水平撞墙＝得分（游戏事件）**，要更新比分并置标志；**垂直撞墙＝单纯反弹**，只翻转方向、当帧不位移。

#### 4.3.3 源码精读

球控制整体在一个 `always_ff` 里用 `case(state)` 分派：[top_pong.sv:L156-L226](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L156-L226)。其中：

- `NEW_GAME` 分支（L158-L161）：清零比分。
- `POSITION` 分支（L163-L179）：重置碰撞标志、球速、击球计数；**根据上一回合的 `coll_r` 决定发球方向**——这里利用了非阻塞赋值的特性：`coll_r <= 0` 在块末才生效，所以 `if (coll_r)` 读到的是 PLAY 阶段写入的旧值。
- `PLAY` 分支里的水平运动与得分（L184-L196）：撞右墙给左方加分 `score_l`、撞左墙给右方加分 `score_r`。
- 垂直运动与上下墙反弹（L199-L207）。
- 击球计数与加速（L210-L215）：`ball_dx_prev != ball_dx` 用来检测「方向变了＝发生一次击球」，到 `SPEEDUP` 次就提速。`ball_spx` 还有个上限 `(ball_spx < PAD_WIDTH) ? ball_spx+1 : ball_spx`，避免球速大于球拍厚度导致「穿透」。

#### 4.3.4 代码实践（源码阅读 + 手算）

1. **目标**：手算一帧球的位移，理解 `frame` 节拍。
2. **步骤**：设 `BALL_SIZE=8`、`BALL_ISPX=5`、`BALL_ISPY=3`、`H_RES=640`、`V_RES=480`，初始 `ball_x=42`（= `PAD_OFFS+PAD_WIDTH`）、`ball_y=236`、`ball_dx=0`（右）、`ball_dy=0`（下）。
3. **观察**：模拟连续 3 个 `frame` 脉冲后 `ball_x`、`ball_y` 的值。
4. **预期结果**：
   - 第 1 帧：`ball_x=47, ball_y=239`
   - 第 2 帧：`ball_x=52, ball_y=242`
   - 第 3 帧：`ball_x=57, ball_y=245`
5. **待本地验证**：可在仿真里打印这三个寄存器确认。

#### 4.3.5 小练习与答案

- **Q1**：为什么垂直撞墙只翻转 `ball_dy`、当帧不让 `ball_y` 位移？
  - **A1**：简化处理。代码里撞墙分支只写 `ball_dy <= ...` 而没有 `ball_y <= ...`，所以这一帧球停在原位、下一帧才反向移动。这是一种可接受的近似（最多差一帧的位移）。
- **Q2**：`ball_spx` 为什么设上限 `PAD_WIDTH`？
  - **A2**：若水平速度大于球拍厚度，球可能在一帧内跨过整个球拍而「穿模」；限制 `ball_spx < PAD_WIDTH`（=10）可缓解。注意它只限制水平速度，是针对水平方向的穿透。

---

### 4.4 命中检测与球拍反弹（含加速）

#### 4.4.1 概念说明

4.3 解决了「球与墙」。本模块解决「球与球拍」：什么时候算撞到球拍？撞到后怎么办？

Pong 用了一个非常巧妙的思路：**让光栅扫描本身充当碰撞传感器**。每帧扫描到某个像素 `(sx,sy)` 时，组合逻辑同时判断「这个像素在不在球矩形里」「在不在球拍矩形里」。如果某像素既在球里又在左球拍里，且球正向左飞，就翻转方向——反弹！这个判断写在时序逻辑里但**不受 `frame` 门控**，于是扫描过程中一旦碰到就立即生效。

#### 4.4.2 核心流程

```
组合（每个像素时钟）：
  ball = (sx∈[ball_x, ball_x+SIZE)) ∧ (sy∈[ball_y, ball_y+SIZE))
  padl = (sx∈[PAD_OFFS, PAD_OFFS+PAD_WIDTH)) ∧ (sy∈[padl_y, padl_y+PAD_HEIGHT))
  padr = (sx∈[右球拍x区间])              ∧ (sy∈[padr_y, padr_y+PAD_HEIGHT))

时序（每个像素时钟，不受 frame 门控）：
  if (ball ∧ padl ∧ ball_dx==1) ball_dx <= 0   // 撞左拍、原本向左 → 改向右
  if (ball ∧ padr ∧ ball_dx==0) ball_dx <= 1   // 撞右拍、原本向右 → 改向左
```

为什么不会在扫描期间反复翻转？因为一旦 `ball_dx` 从 1 翻成 0，条件 `ball_dx==1` 立刻变假，本帧剩余重叠像素不再触发；反向同理。

#### 4.4.3 源码精读

命中检测是纯组合的四不等式（典型 AABB「点是否在矩形内」）：[top_pong.sv:L229-L236](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L229-L236)。`ball`、`padl`、`padr` 既是「命中标志」也是「绘制标志」（后面上色要用）。

反弹触发写在球控制 `always_ff` 的末尾、`case(state)` 之外、`if(frame)` 之外：[top_pong.sv:L220-L222](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L220-L222)。这两行是本讲最精妙之处——**用渲染扫描顺便完成了碰撞响应**，无需独立的碰撞检测时钟。

紧接着的 `if (frame) ball_dx_prev <= ball_dx;`（[L225](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L225)）记录上一帧的方向，配合 4.3 的 `ball_dx_prev != ball_dx` 用来统计击球次数。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「扫描即检测」如何避免重复反弹。
2. **步骤**：读 L220-L222，假设球与左球拍在某帧扫描中有 20 个重叠像素。
3. **观察**：球正向左（`ball_dx=1`）扫到第 1 个重叠像素时 `ball_dx` 变 0；剩下的 19 个重叠像素里 `ball_dx==1` 已不成立。
4. **预期结果**：`ball_dx` 在该帧只翻转一次（1→0），不会抖动。
5. **待本地验证**：可在仿真波形里观察 `ball_dx` 在一帧内只跳变一次。

#### 4.4.5 小练习与答案

- **Q1**：若把反弹条件里的 `&& ball_dx==1` 去掉，会发生什么？
  - **A1**：只要球与球拍重叠就持续翻转方向，`ball_dx` 会在扫描的重叠像素间反复跳变，导致行为不确定、球可能「卡」在球拍里。
- **Q2**：为什么反弹逻辑放在 `case(state)` 和 `if(frame)` 之外？
  - **A2**：反弹必须在扫描期间即时响应（球只在一帧的某些像素处与球拍重叠），所以不能等 `frame` 脉冲；且它对所有游戏状态都应生效（只要在 `PLAY` 实际有意义，因为只有 PLAY 时球才在飞）。

---

### 4.5 球拍控制：玩家与 AI

#### 4.5.1 概念说明

Pong 是单人对 AI 的版本：左球拍是玩家（`play_y`），右球拍是 AI（`ai_y`）。两个球拍都只能在垂直方向移动，移动同样受 `frame` 节拍（每帧最多移 `PAD_SPY` 像素），并都做了「撞屏幕上下边沿则贴边」的限位。

玩家与 AI 的差别只在「移不移动」的判断条件：玩家看消抖后的按键脉冲；AI 看「球当前在球拍上方还是下方」来自动追踪。

#### 4.5.2 核心流程

```
POSITION 状态：play_y、ai_y 都初始化到屏幕垂直中央。

PLAY 状态、每帧（frame）：
  AI：比较「球中心」与「球拍中心」：
      球在球拍下方 → 向下移 PAD_SPY（贴下限）
      球在球拍上方 → 向上移 PAD_SPY（贴上限）
      否则不动
  玩家：
      按 down → 向下移 PAD_SPY（贴下限）
      按 up   → 向上移 PAD_SPY（贴上限）
      都没按 → 不动
```

AI 的「聪明程度」由 `PAD_SPY` 与球速的相对大小决定——`PAD_SPY=3`、初始 `BALL_ISPY=3`，AI 勉强跟得上垂直分量，但水平方向飞过来时它会追，给人一种「还挺好打」的对手感。

#### 4.5.3 源码精读

两个球拍绑定到玩家/AI（`padl_y=play_y`、`padr_y=ai_y`）的纯组合：[top_pong.sv:L87-L90](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L87-L90)。这种「逻辑角色 vs 物理球拍」分离让你很容易把双人改成「左 AI 右玩家」等组合。

AI 追球：[top_pong.sv:L124-L137](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L124-L137)。它用 `ai_y + PAD_HEIGHT/2`（球拍中心）与 `ball_y`（球顶部）比较；当球在下方时 `ai_y + PAD_HEIGHT/2 < ball_y`，向下追。

玩家按键控制：[top_pong.sv:L140-L153](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L140-L153)。结构几乎与 AI 相同，只是把「球在哪」换成「按了哪个键」。三个按键先经 `debounce` 消抖（[L94-L98](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L94-L98)），其中 fire 取 `onup`（松开脉冲）、up/down 取 `out`（去抖电平）。

#### 4.5.4 代码实践（修改型）

1. **目标**：体会 AI 难度与 `PAD_SPY` 的关系。
2. **步骤**：把 AI 分支里的 `PAD_SPY` 改成 `1`（其余不变），重新仿真或综合。
3. **观察**：AI 球拍每帧最多只移 1 像素，跟不上球速 3 的垂直分量。
4. **预期结果**：AI 明显变弱、漏球增多，玩家更容易得分。
5. **待本地验证**：在 SDL 仿真里打几局感受差异。

#### 4.5.5 小练习与答案

- **Q1**：怎样把本设计改成「双人对战」（右球拍也由按键控制）？
  - **A1**：新增一组 up/down 按键（如 `btn_up2/btn_dn2`）并消抖，把 `ai_y` 的 `always_ff` 改成读这组按键、逻辑照抄玩家分支即可；或干脆把 `padr_y` 也绑到一个新的 `play2_y`。
- **Q2**：AI 为什么用 `ai_y + PAD_HEIGHT/2 < ball_y`（球拍中心 vs 球顶部）而不是严格的球中心比较？
  - **A2**：这是一种略偏宽松的追踪启发式，省去 `BALL_SIZE/2` 的加法、简化比较；代价是 AI 中心对齐的是球顶而非球心，影响很小。

---

### 4.6 字形 ROM 计分：simple_score

#### 4.6.1 概念说明

分数（0~9）需要「画」到屏幕上。`simple_score` 用一个最朴素的方法：把 0~9 每个数字预先定义成一个 3 列 × 5 行 = 15 像素的点阵（位图），存在一个 10 元素的数组里——这就是一个微型 ROM。显示时，根据当前扫描坐标 `(sx,sy)` 算出「如果落在分数显示区，对应字形的第几个像素」，查表输出 1/0。

为让数字清晰，点阵被放大 4 倍（每个点阵像素对应屏幕 4×4 区域），所以屏幕上每个数字占 12×20 像素。

#### 4.6.2 核心流程

```
组合：
  1. 把 score_l/score_r 钳位到 0~9（chars 数组只有 10 项）
  2. 判断 (sx,sy) 是否落在左上 / 右上两个 12×20 显示区
  3. 若落在，把 (sx,sy) 映射成字形内的线性地址 pix_addr = (列)/4 + 3*(行)/4
时序（打一拍）：
  4. pix <= chars[对应数字][pix_addr]   // 该像素是否点亮
```

注意第 4 步是 `always_ff`（寄存输出，1 拍延迟），所以第 2、3 步的坐标判断用了 `sx-7` 而非 `sx-8`（提前 1 拍）来补偿——这正是 u5-l3 讲过的「同步读 ROM 有一拍延迟」的体现。

#### 4.6.3 源码精读

10 个数字的 3×5 点阵（`logic [0:14] chars [10]`，MSB first 以便从左到右写像素）：[simple_score.sv:L22-L35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_score.sv#L22-L35)。例如 `chars[0] = 15'b111_101_101_101_111` 就是一个「口」字形 0。这部分是纯组合的常量数组，综合后会被推断为 LUTROM 或分布式 RAM。

分数钳位与显示区判定：[simple_score.sv:L38-L50](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_score.sv#L38-L50)。左分数区在 `(7≤sx<19, 8≤sy<28)`，右分数区镜像在屏幕右侧 `H_RES-22 ≤ sx < H_RES-10`。

地址映射与寄存输出：[simple_score.sv:L53-L67](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/simple_score.sv#L53-L67)。`pix_addr` 的计算 `(sx-7)/4 + 3*((sy-8)/4)` 中，`/4` 是 4 倍缩放、`3*` 是每行 3 个像素。

最后，顶层把 `pix_score` 接进上色优先级：分数优先于球、球优先于球拍、球拍优先于背景：[top_pong.sv:L251-L256](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L251-L256)。颜色用 12 位 `{R,G,B}`：分数 `0xF30`（橙红）、球 `0xFC0`（黄）、球拍 `0xFFF`（白）、背景 `0x137`（深蓝）。

消隐期强制涂黑并寄存输出到 VGA 引脚：[top_pong.sv:L260-L273](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L260-L273)。

> **平台差异提示**：720p 版（`xc7-dvi/top_pong.sv`）的游戏参数整体放大约 2 倍（`BALL_SIZE=16`、`PAD_HEIGHT=96`、`BALL_ISPX=10` 等，见 [xc7-dvi/top_pong.sv:L25-L33](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7-dvi/top_pong.sv#L25-L33)），并把最后的 VGA 引脚换成 DVI/HDMI 差分输出（经 `dvi_generator` 做 TMDS 编码与串行化，[L280-L307](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7-dvi/top_pong.sv#L280-L307)，衔接 u6-l2）。仿真版（`sim/top_pong.sv`）则把输出接到 SDL，并把 4 位颜色复制成 8 位（`{2{display_r}}`，[sim/top_pong.sv:L256-L263](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/top_pong.sv#L256-L263)）。

#### 4.6.4 代码实践（阅读 + 手画字形）

1. **目标**：看懂 3×5 字形编码。
2. **步骤**：读 `simple_score.sv` L25-L34 的 `chars[0..9]`。
3. **观察**：把 `chars[2] = 15'b111_001_111_100_111` 按 3 列 × 5 行展开成点阵。
4. **预期结果**：

   ```
   1 1 1
   0 0 1
   1 1 1
   1 0 0
   1 1 1
   ```

   正是一个「2」字。
5. **思考**：若想显示字母（比如赢了显示 `WIN`），你会怎么扩展这个表？（把 `chars` 扩成更多项、地址改宽即可。）

#### 4.6.5 小练习与答案

- **Q1**：为什么 `simple_score` 要把输出寄存一拍（`always_ff`），而命中检测（`ball`/`padl`/`padr`）用组合？
  - **A1**：字形查表路径较长（地址计算 + 表查），寄存一拍有助于时序收敛；代价是引入 1 拍延迟，故坐标判断提前 1 拍补偿。命中检测要参与「扫描即反弹」（4.4），必须当拍可用，所以用组合。
- **Q2**：`pix_addr = (sx-7)/4 + 3*((sy-8)/4)` 里的 `3*` 从哪来？
  - **A2**：字形每行 3 个像素，`(sy-8)/4` 是行号，乘以每行像素数 3 得到该行起始地址，再加列号 `(sx-7)/4`。

---

## 5. 综合实践：跑通 Pong 并改造它

本实践是本讲的主体任务，分三条路线，**任选其一**（推荐仿真路线，门槛最低）。

### 实践目标

- 在 Verilator 仿真或开发板上看到 Pong 真正运行。
- 在源码里精确定位「球拍移动」「球反弹」「计分加一」三段逻辑。
- 修改球速或球拍尺寸，预测并观察游戏行为变化。

### 路线 A：Verilator + SDL 仿真（推荐，无需开发板）

依赖：C++ 工具链、Verilator（≥4.038）、SDL2。Linux 安装：

```shell
apt update
apt install build-essential verilator libsdl2-dev
```

构建与运行（参见 [sim/README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/README.md) 与 [sim/Makefile](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/Makefile)）：

```shell
cd ThreePart/projf-explore/graphics/pong/sim
make pong
./obj_dir/pong
```

按键：`↑`/`↓` 移动球拍，`空格` 发球/确认，`Q` 退出。注意仿真顶层 [sim/top_pong.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/top_pong.sv) 的游戏逻辑与 VGA 版完全一致，C++ 侧 [main_pong.cpp](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/main_pong.cpp) 只负责每个像素时钟把 `sdl_r/g/b` 写进帧缓冲、每帧读一次键盘（[main_pong.cpp:L76-L114](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/sim/main_pong.cpp#L76-L114)）。

### 路线 B：Arty 开发板上板（VGA）

在 Vivado Tcl 控制台（参见 [README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/README.md)）：

```tcl
cd ThreePart/projf-explore/graphics/pong/xc7/vivado
source ./create_project.tcl
```

按 `BTN2`=上、`BTN1`=发球、`BTN0`=下。`create_project.tcl`（[xc7/vivado/create_project.tcl:L57-L63](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/vivado/create_project.tcl#L57-L63)）只纳入 4 个源：`top_pong.sv`、`clock_480p.sv`、`debounce.sv`、`simple_480p.sv`、`simple_score.sv`——可以清楚看到 Pong 实际依赖的库模块极少。

### 路线 C：纯源码阅读（无任何工具）

直接精读 `xc7/top_pong.sv`，对照本讲 4.2~4.6 的行号定位逻辑。

### 三段逻辑定位任务（三条路线都要做）

无论走哪条路线，请在 `xc7/top_pong.sv` 里找到并用自己的话复述：

1. **球拍移动**：玩家球拍控制——[L140-L153](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L140-L153)（AI 在 [L124-L137](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L124-L137)）。
2. **球反弹**：球拍碰撞翻转方向——[L220-L222](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L220-L222)。
3. **计分加一**：撞墙得分——`score_l+1` 在 [L187](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L187)、`score_r+1` 在 [L193](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L193)。

### 改造实验

选一个改动，先写下你的**预测**，再运行确认：

| 改动 | 改哪里 | 预期现象 |
|------|--------|----------|
| 球变快 | `BALL_ISPX`/`BALL_ISPY`（[L25-L26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L25-L26)）调大 | 球初始就快，AI 更容易漏 |
| 球拍变长 | `PAD_HEIGHT`（[L27](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L27)）调大（如 96） | 更好接球，游戏变简单 |
| 不加速 | 把 `SPEEDUP` 改成一个超大值（如 999） | 球速恒定，不掉速 |
| 改胜分 | `WIN`（[L22](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/pong/xc7/top_pong.sv#L22)）改成 1 | 一球定胜负 |

> 注意：`score_l/score_r` 是 4 位（最大 9），所以 `WIN` 最大只能设 9。

### 预期结果

- 仿真/上板能看到深蓝背景、白色球拍、黄色球、橙红分数；按空格发球，球在两拍间往返、撞墙得分、撞拍反弹；先到 4 分进入 `END_GAME`，再按空格开新局。
- 改动后现象与你的预测一致；若不一致，回到对应行号排查（常见误区：忘了 `frame` 节拍、忘了非阻塞赋值的「读旧值」特性）。
- **若你无法运行仿真/上板**：以上为「待本地验证」，请以源码阅读 + 手算为准。

---

## 6. 本讲小结

- **简化显示时序** `simple_480p/720p` 用无符号坐标、有效区在原点，让游戏坐标（`ball_x` 等）可直接与 `sx/sy` 比较；480p 负极性同步、720p 正极性同步。
- **六状态机**（`NEW_GAME/POSITION/READY/PLAY/POINT/END_GAME`）以两段式 Moore 结构管理一局游戏，并在 PLL 未锁定时强制回 `NEW_GAME` 兜底。
- **帧节拍 `frame`**（每帧亮一拍）把 60 Hz 游戏逻辑与 25 MHz 像素渲染解耦；球/球拍位置只在 `frame` 为真的那一拍更新。
- **碰撞检测**用 AABB 四不等式纯组合实现；**最巧妙**的是让光栅扫描本身充当传感器——反弹逻辑写在 `always_ff` 里但不受 `frame` 门控，扫描到重叠像素且方向相向时即时翻转。
- **球拍**分玩家（按键）与 AI（追球中心），结构同构；AI 难度由 `PAD_SPY` 与球速的相对大小决定。
- **计分**用 3×5 字形 ROM（4 倍放大为 12×20），输出寄存一拍并提前 1 拍补偿坐标；上色按「分数>球>球拍>背景」优先级。游戏核心在四个 `top_pong.sv` 里逐字节相同，只有 I/O 包装与参数缩放不同。

## 7. 下一步学习建议

- **横向对比更复杂的 demo**：读 [u8-l4](u8-l4-demos-analysis.md) 涉及的 `life-on-screen`（元胞自动机）与 `ad-astra`（星空精灵），看它们如何复用同一套 `lib/display`+`lib/graphics`+`lib/memory` 搭出完全不同的画面，体会「显示时序 + 坐标比较」这一范式的可扩展性。
- **给 Pong 加功能**：尝试加一条中线（在 `paint` 优先级里对 `sx == H_RES/2` 涂白）、给球加旋转/角度反弹（根据撞击点在球拍上的相对位置改变 `ball_dy`）、或把双人改成双人对战。
- **深入显示链路**：若你对 720p 版的 DVI/HDMI 输出感兴趣，回顾 [u6-l2 DVI/HDMI 与 TMDS 编码](u6-l2-dvi-hdmi-tmds.md)，对照 `xc7-dvi/top_pong.sv` 末尾的 `dvi_generator` 与 `tmds_out` 看逻辑视频如何变成差分电平。
- **回归数学**：本讲的坐标比较、`/4` 缩放、`(V_RES-PAD_HEIGHT)/2` 居中都是简单算术；若想做带物理感的游戏（加速度、抛物线），可继续学 [Unit 7 FPGA 数学运算](u7-l1-number-representation.md) 的定点数与乘除法。
