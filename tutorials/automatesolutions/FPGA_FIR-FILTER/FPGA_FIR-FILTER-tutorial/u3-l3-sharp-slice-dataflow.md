# 颜色通道切片 sharp_slice 的二维滤波数据流

## 1. 本讲目标

本讲把 u3-l1 里一直当作“黑盒”的 `sharp_slice` 打开。回忆 u3-l1 的结论：顶层 `sharp` 对 R/G/B 各例化一个 `sharp_slice`，把单通道像素 `data_in` 喂进去，得到锐化后的 `data_out`。至于 `sharp_slice` 内部怎么把一个像素变成“二维锐化后的像素”，u3-l1 故意没展开——那是本讲的主题。

读完本讲，你应当能够：

- 看懂 `sharp_slice` 内部如何用 **6 个 `sharp_linemem` 级联**出 7 个**垂直抽头**（`v_tap`），让同一列、相邻 7 行的像素“同时”出现在 `sharp_arith` 的输入端；
- 看懂它又如何用一组**水平移位寄存器**出 7 个**水平抽头**（`h_tap`），让同一行、相邻 7 列的像素同时出现在第二个 `sharp_arith` 的输入端；
- 说清**垂直 `sharp_arith` 与水平 `sharp_arith` 两次串联**如何对应 u2-l1 讲的“可分离二维滤波”，并把每个抽头对应到相对中心像素的空间位置；
- 在纸上画出 `v_tap`、`h_tap` 的数据流图，标出每个抽头相对中心像素“偏上/偏下几行、偏左/偏右几列”。

本讲只讲**数据流的几何结构**（哪个抽头是哪一格像素），`sharp_linemem` 循环缓冲的地址管理细节留到 u4-l1，`sharp_arith` 定点乘加/饱和截断的细节留到 u4-l2。

## 2. 前置知识

本讲承接前几讲，不再重复其结论，只直接使用：

- **u2-l1 图像锐化与可分离 FIR**：锐化核为 \([1,0,-9,48,-9,0,1]/32\)；二维卷积等价于“先垂直、再水平”两次一维卷积，两个方向共用同一组系数。本讲就是这套数学的硬件实现。
- **u2-l2 定点系数设计**：系数 \([1,0,-9,48,-9,0,1]/32\) 来自 `round(32*fir1(8,0.5,"high"))` 并叠加恒等核；其中 `tap_m2`/`tap_p2` 处系数恰为 0，所以硬件里这两格被省略不乘。
- **u3-l1 顶层 sharp.vhd**：你已经知道 `sharp_slice` 的端口是 `data_in`/`data_out`（`integer 0..255`）加 `clk`/`reset`/`de_in`；其中 `de_in` 在内部被当作行存储的写使能。本讲从这组端口往里走。

此外需要两个 VHDL 概念（不熟悉的术语下面会解释）：

- **`generate` 循环**：用 `for i in 0 to N generate` 在综合期“复制”出 N 份结构相同的硬件，每份的端口连线随 `i` 变化。它不是运行期循环，而是“批量画电路”。
- **数组型 `signal` 与移位寄存器**：把数组 `a(0..6)` 声明成 `signal`，在时钟进程里写 `a(0)<=x; a(i+1)<=a(i);`，综合后每个数组元素都是一个触发器，整体构成一条移位链。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `FPGA-Design/sharp_slice.vhd` | 单颜色通道的二维可分离锐化（本讲主角） | 逐段精读三条数据通路 |
| `FPGA-Design/sharp_linemem.vhd` | 1280 项行存储，把信号延迟“一整行” | 只用“延迟 1 行”这一结论；地址循环细节留 u4-l1 |
| `FPGA-Design/sharp_arith.vhd` | 7 抽头定点乘加 + 饱和截断 | 只用“同一组系数、对称 7 抽头”这一结论；运算细节留 u4-l2 |

> 提示：本讲会引用 `sharp_arith.vhd` 第 27 行的系数注释和第 34 行的 `sum` 表达式，但**只为了确认两组抽头共用同一组系数**；舍入 `+16` 与饱和截断的完整讲解在 u4-l2。

## 4. 核心概念与源码讲解

先看一张 `sharp_slice` 的内部总览，后续三个最小模块分别对应图中的三段：

```
                                   ┌─ v_tap(0..6) ─┐
 data_in ──► ① 垂直抽头链 ───────► │ 7 个垂直抽头  │ ──► ┌────────────┐
            (6 个 linemem 级联)    └───────────────┘     │ ver_filt    │──► v_out
              每级延迟 1 行                                │ sharp_arith │
                                                          └────────────┘
                                                                 │
                                                                 ▼
                                   ┌─ h_tap(0..6) ─┐      ② 水平抽头链
                            ┌────► │ 7 个水平抽头  │ ──► ┌────────────┐
                            │      └───────────────┘     │ hor_filt    │──► data_out
                        ② 水平移位寄存器(6 级触发器)      │ sharp_arith │
                            每级延迟 1 拍(=1 列)           └────────────┘
```

一句话概括：**先用 6 个行存储级联，得到“同一列、7 行”的垂直抽头，做一次一维锐化；再把这次的结果送进一条水平移位寄存器，得到“同一行、7 列”的水平抽头，做第二次一维锐化。** 两次一维锐化串联 = 一次 7×7 二维锐化，这正是可分离滤波的硬件形态。

下面按 ① 垂直抽头链 → ② 水平抽头链 → ③ 两次 arith 串联 的顺序拆开讲。

---

### 4.1 垂直抽头链（linemem 级联）

#### 4.1.1 概念说明

垂直方向的卷积，需要用到**同一列、上下相邻 7 行**的像素。问题在于：视频是按光栅顺序逐行扫描送进来的，任意一个时刻，输入端 `data_in` 只有“当前这一格”像素。那么“上一行同一列”“上上行同一列”这些像素从哪里来？

答案是**行存储（line buffer）**：把整行像素按顺序存进一块 RAM，等下一行扫到同一列时再读出来——这样读出的值正好是“上一行同列像素”。一块行存储 = 把信号**延迟一整行**。要凑齐 7 行，就需要把 6 块行存储**级联**起来：每经过一块，信号就再老一行；于是在级联链的 7 个抽头点上，能同时拿到当前行、上一行、…、上 6 行同列的 7 个像素。

> 为什么是 6 块而不是 7 块？因为“当前行”本身不需要延迟，直接取 `data_in` 就是第 0 个抽头 `v_tap(0)`；要让中心抽头落在 7 个抽头的正中间（第 4 个，索引 3），还需要 6 块行存储补出抽头 1~6。

这一段的关键直觉：**垂直抽头链 = 一条“行延迟阶梯”，每一阶把信号往更旧的行推一行，阶梯上的 7 个抽头同时覆盖 7 行。**

#### 4.1.2 核心流程

设当前输入像素位于第 \(R\) 行第 \(C\) 列（行号向下增长）。每块 `sharp_linemem` 把信号延迟“1 行”（即 1280 个有效像素，正好是 720p 一行的有效像素数，见 u4-l1）。级联后的 7 个垂直抽头为：

| 抽头 | 来源 | 相对输入的行延迟 | 对应像素行（同列 \(C\)） | 送入 ver_filt 的端口 |
| --- | --- | --- | --- | --- |
| `v_tap(0)` | `data_in`（组合连线，不延迟） | 0 行 | 第 \(R\) 行（最新） | `tap_m3` |
| `v_tap(1)` | linemem 0 输出 | 1 行 | 第 \(R-1\) 行 | `tap_m2` |
| `v_tap(2)` | linemem 1 输出 | 2 行 | 第 \(R-2\) 行 | `tap_m1` |
| `v_tap(3)` | linemem 2 输出 | 3 行 | 第 \(R-3\) 行（**垂直中心**） | `tap_00` |
| `v_tap(4)` | linemem 3 输出 | 4 行 | 第 \(R-4\) 行 | `tap_p1` |
| `v_tap(5)` | linemem 4 输出 | 5 行 | 第 \(R-5\) 行 | `tap_p2` |
| `v_tap(6)` | linemem 5 输出 | 6 行 | 第 \(R-6\) 行（最旧） | `tap_p3` |

因为锐化核 \([1,0,-9,48,-9,0,1]\) 关于中心对称，“m/p”只是给对称位置贴的标签，方向并不影响结果——重要的是 `v_tap(3)` 被当成中心（`tap_00`），两侧各 3 个抽头对称分布。

把数据流画成阶梯：

```
data_in ──┬─ v_tap(0)=tap_m3 (row R)
          │
          ▼
       [linemem 0]  ──► v_tap(1)=tap_m2 (row R-1)
          │
          ▼
       [linemem 1]  ──► v_tap(2)=tap_m1 (row R-2)
          │
          ▼
       [linemem 2]  ──► v_tap(3)=tap_00 (row R-3)  ← 垂直中心
          │
          ▼
       [linemem 3]  ──► v_tap(4)=tap_p1 (row R-4)
          │
          ▼
       [linemem 4]  ──► v_tap(5)=tap_p2 (row R-5)
          │
          ▼
       [linemem 5]  ──► v_tap(6)=tap_p3 (row R-6)

 每经过一块 [linemem]，信号延迟 1 行；7 个 v_tap 同时送到 ver_filt (sharp_arith)。
```

#### 4.1.3 源码精读

`sharp_slice` 先声明两个 7 元数组，分别装垂直、水平抽头：[FPGA-Design/sharp_slice.vhd:20-23](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L20-L23) —— `filter_array is array (0 to 6) of integer range 0 to 255`，`v_tap`/`h_tap` 都是这种 7 格数组，外加一个中间结果 `v_out`。

第 0 个垂直抽头直接取输入，是一根**组合连线**（在进程外用连续赋值）：[FPGA-Design/sharp_slice.vhd:27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L27)，`v_tap(0) <= data_in;`。所以 `v_tap(0)` 没有任何寄存器延迟，就是当前像素本身。

接着用 `generate` 循环“批量画”6 块级联行存储：[FPGA-Design/sharp_slice.vhd:29-36](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36)。展开后等价于：

```vhdl
-- i=0: linemem 的输入是 v_tap(0)，输出是 v_tap(1)
-- i=1: linemem 的输入是 v_tap(1)，输出是 v_tap(2)
-- ...
-- i=5: linemem 的输入是 v_tap(5)，输出是 v_tap(6)
mem_i: entity work.sharp_linemem
    port map ( clk      => clk,
               reset    => reset,
               write_en => de_in,      -- 只在有效像素时推进行存储
               data_in  => v_tap(i),
               data_out => v_tap(i+1));
```

读这段时要抓三点：

1. **级联关系**：第 `i` 级的 `data_in` 接的是 `v_tap(i)`，`data_out` 产出 `v_tap(i+1)`；而 `v_tap(i)` 又是第 `i-1` 级的输出。所以 6 块行存储首尾相接，是一条真正的级联链，不是 6 个并行存储。
2. **写使能 `write_en => de_in`**：行存储只在 `de_in='1'`（有效像素）时读写推进，消隐期不动。这正是顶层把 `de_0` 喂给三个 `sharp_slice` 共享的原因（见 u3-l1 §4.2）。
3. **每块 = 1 行延迟**：`sharp_linemem` 内部是一块 1280 项 RAM 的循环缓冲（[FPGA-Design/sharp_linemem.vhd:20](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_linemem.vhd#L20) `array (0 to 1279)`），写当前像素、同时读出 1280 个有效像素之前写入的旧像素（[sharp_linemem.vhd:31-34](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_linemem.vhd#L31-L34)）。1280 正好是 720p 一行的有效像素数，所以“延迟 1280 个有效像素”=“延迟一整行”。地址如何循环回绕是 u4-l1 的主题，这里只用结论。

最后，7 个垂直抽头按对称顺序送进垂直滤波器：[FPGA-Design/sharp_slice.vhd:39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49)，端口映射把 `v_tap(0..6)` 分别接到 `tap_m3, tap_m2, tap_m1, tap_00, tap_p1, tap_p2, tap_p3`，`v_tap(3)` 落在中心 `tap_00`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认“6 块行存储级联 → 7 个垂直抽头”，并定位垂直中心抽头。
2. **步骤**：
   - 打开 [sharp_slice.vhd:29-36](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36)，确认 `generate` 的范围是 `0 to 5`，即 **6** 个实例。
   - 看 [sharp_slice.vhd:20](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L20)，确认 `v_tap` 数组是 `0 to 6`，即 **7** 个抽头。
   - 看 [sharp_slice.vhd:42-48](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L42-L48)，确认 `v_tap(3)` 被映射到 `tap_00`（中心）。
3. **预期结果**：6 块行存储 + 1 根组合连线 = 7 个抽头；中心抽头 `v_tap(3)` 相对输入延迟 3 行，对应像素位于第 \(R-3\) 行。这是纯静态阅读，结论确定，无需“待本地验证”。

#### 4.1.5 小练习与答案

1. **问**：为什么垂直抽头链用 6 块 `sharp_linemem`，而不是 7 块？
   **答**：第 0 个抽头 `v_tap(0)` 直接取 `data_in`（一行组合连线，无延迟），不需要行存储。要让 7 个抽头关于中心 `v_tap(3)` 对称（中心两侧各 3 个），只需 6 块行存储补出抽头 1~6。

2. **问**：如果把 `generate` 的范围从 `0 to 5` 改成 `0 to 3`，垂直滤波会变成什么样？
   **答**：只剩 4 块行存储，产出抽头 0~4 共 5 个。`sharp_arith` 仍有 7 个抽头端口，`v_tap(5)`/`v_tap(6)` 会变成未驱动（综合报错或悬空），设计不再正确。正确做法是“抽头数”与“系数个数”必须同时改——这正是 u6-l3 二次开发要处理的事。

3. **问**：`v_tap(0)` 到 `v_tap(6)` 这 7 个信号，在任意一个时钟沿代表的是“同一行的 7 个像素”还是“同一列的 7 个像素”？
   **答**：**同一列、7 行**。因为每块行存储延迟的是“一整行”，所以相邻抽头相差一行、列号相同；这正是垂直（行间）卷积所需要的输入。

---

### 4.2 水平抽头链（移位寄存器）

#### 4.2.1 概念说明

垂直滤波之后得到中间结果 `v_out`，它仍是按列逐格流出的（当前拍对应第 \(C\) 列）。水平方向的卷积需要用到**同一行、左右相邻 7 列**的 `v_out` 值。

这一次不需要昂贵的行存储了：因为同一行的相邻像素在时间上只差几个时钟周期（一个像素一个时钟），只要把 `v_out` 送进一条**移位寄存器**，每拍整体右移一格，就能在寄存器链的 7 个抽头点上同时拿到“当前列、左 1 列、…、左 6 列”的 `v_out`。

对比两种抽头链的代价很有意思：

- **垂直方向**：相邻样本在时间上相隔“一整行”，必须用大块 RAM（行存储）才能把它们重新对齐到同一时刻；
- **水平方向**：相邻样本在时间上只相隔“一个时钟”，用几个触发器（移位寄存器）就够了。

这正是可分离滤波的另一层好处：两个方向虽然都用 7 抽头，但存储代价天差地别——行存储（RAM）几乎全花在垂直链上，水平链只是一串触发器。u1-l3 提到的“片上存储约占 16%”，主要就是这 6×3=18 块行存储（垂直链 × RGB 三通道）。

#### 4.2.2 核心流程

设 `v_out` 当前拍对应第 \(C\) 列。每个时钟沿，`v_out` 进入 `h_tap(0)`，链上内容整体右移一格。所以 `h_tap(i)` = i 拍之前的 `v_out` = 第 \(C-i\) 列的 `v_out`。7 个水平抽头为：

| 抽头 | 来源 | 相对当前 `v_out` 的延迟 | 对应列（同一行） | 送入 hor_filt 的端口 |
| --- | --- | --- | --- | --- |
| `h_tap(0)` | 本拍 `v_out` | 0 拍 | 第 \(C\) 列（最新，最右） | `tap_m3` |
| `h_tap(1)` | 1 拍前 | 1 拍 | 第 \(C-1\) 列 | `tap_m2` |
| `h_tap(2)` | 2 拍前 | 2 拍 | 第 \(C-2\) 列 | `tap_m1` |
| `h_tap(3)` | 3 拍前 | 3 拍 | 第 \(C-3\) 列（**水平中心**） | `tap_00` |
| `h_tap(4)` | 4 拍前 | 4 拍 | 第 \(C-4\) 列 | `tap_p1` |
| `h_tap(5)` | 5 拍前 | 5 拍 | 第 \(C-5\) 列 | `tap_p2` |
| `h_tap(6)` | 6 拍前 | 6 拍 | 第 \(C-6\) 列（最旧，最左） | `tap_p3` |

同样因核对称，左右方向不影响结果；关键仍是 `h_tap(3)` 落在中心 `tap_00`，两侧各 3 个对称分布。

数据流：

```
            (本拍)       (1拍前)      (2拍前)       (3拍前)        (6拍前)
 v_out ──► h_tap(0) ──► h_tap(1) ──► h_tap(2) ──► h_tap(3) ──►···──► h_tap(6)
   │        =tap_m3     =tap_m2     =tap_m1      =tap_00         =tap_p3
   │        col C       col C-1     col C-2      col C-3  ←中心   col C-6
   │
   └─ 每个上升沿，整条链右移一格；7 个 h_tap 同时送到 hor_filt (sharp_arith)。
```

#### 4.2.3 源码精读

水平移位用一个时钟进程实现：[FPGA-Design/sharp_slice.vhd:51-58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58)：

```vhdl
process
begin
  wait until rising_edge(clk);
  h_tap(0) <= v_out;            -- 新样本进链头
  for i in 0 to 5 loop
    h_tap(i+1) <= h_tap(i);     -- 链上内容整体右移一格
  end loop;
end process;
```

注意这里和垂直链的三个对照点：

1. **这里是运行期 `for` 循环（在进程里），不是 `generate`**。进程里的 `for i in 0 to 5 loop` 描述的是“在同一时钟沿并发执行的 6 条赋值”，综合成 6 个触发器组成的移位链；而 §4.1 的 `generate` 是综合期复制硬件。两者长得像，语义完全不同。
2. **移位深度也是 6**：循环 `0 to 5` 产出 `h_tap(1..6)` 共 6 级，加上链头 `h_tap(0)`，正好 7 个抽头，与垂直链一一对应。
3. **没有 `de_in` 门控**：水平链对每个时钟沿都移位。消隐期 `v_out` 的值无意义，但让链照常移位不影响有效区结果——有效像素到达时，链里装的就是有效区的历史值。

随后 7 个水平抽头按同样的对称顺序送进第二个 `sharp_arith`：[FPGA-Design/sharp_slice.vhd:60-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70)，`h_tap(0..6)` → `tap_m3, tap_m2, tap_m1, tap_00, tap_p1, tap_p2, tap_p3`，`h_tap(3)` 落在中心 `tap_00`，输出 `data_out`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：确认水平移位寄存器产出 7 个抽头，并定位水平中心抽头。
2. **步骤**：
   - 读 [sharp_slice.vhd:51-58](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58)，确认循环 `0 to 5` = 6 级移位，加链头共 7 个 `h_tap`。
   - 读 [sharp_slice.vhd:63-69](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L63-L69)，确认 `h_tap(3)` 映射到 `tap_00`。
3. **预期结果**：水平中心抽头 `h_tap(3)` = 3 拍前的 `v_out` = 第 \(C-3\) 列；与垂直中心 `v_tap(3)`（第 \(R-3\) 行）一起，决定了输出像素的空间位置（见 §4.3）。纯静态阅读，结论确定。

#### 4.2.5 小练习与答案

1. **问**：为什么水平抽头链用“移位寄存器”就够，而垂直抽头链必须用“行存储”？
   **答**：水平相邻像素在时间上只差 1 个时钟，几个触发器就能把它们对齐；垂直相邻（同列上一行）像素在时间上差“一整行”，必须用容量为一行（1280 项）的 RAM 才能重新对齐。

2. **问**：进程里 `for i in 0 to 5 loop h_tap(i+1) <= h_tap(i);` 是顺序执行还是并发执行？综合出几个触发器？
   **答**：**并发执行**。时钟进程里的多条 `signal` 赋值在上升沿同时生效（用的是信号“旧值”），所以这是 6 条并发赋值，综合出 6 个触发器，构成一条 6 级移位链。若误当成顺序执行，会以为数据一拍内穿过整条链——那是错的。

3. **问**：水平移位进程没有 `if de_in='1'` 的门控，消隐期也在移位，会不会污染有效区的结果？
   **答**：不会。消隐期移进来的 `v_out` 无意义，但当有效像素重新流入时，移位链会被有效值逐步填满；等到中心抽头 `h_tap(3)` 输出有效结果时，链里 7 格已全是有效区的历史值。与行存储不同，这里无需门控。

---

### 4.3 两次 arith 串联：可分离二维滤波的硬件形态

#### 4.3.1 概念说明

把 §4.1 和 §4.2 拼起来，就看到本讲的核心：**两次一维锐化串联 = 一次二维锐化**，这正是 u2-l1 讲的“可分离滤波”在硬件上的样子。

数学上，二维可分离卷积恒等式为：若二维核是某个一维核 \(a\) 与自身的外积（即 \(K_{j,k}=a_j \cdot a_k\)），则

\[
\text{二维卷积} = \text{先按列方向做一维卷积} \;+\; \text{再按行方向做一维卷积}
\]

本项目的 1D 核是 \(a=[1,0,-9,48,-9,0,1]/32\)（半径 3）。硬件里：

- **垂直 `sharp_arith`**（`ver_filt`）做第一次一维卷积：对 7 个垂直抽头加权求和，得到中间结果 `v_out`。它在“行方向”上把 7 行压成 1 个值，相当于把每个像素替换成“本列上下 7 行的锐化结果”。
- **水平 `sharp_arith`**（`hor_filt`）做第二次一维卷积：对 7 个水平抽头（也就是 `v_out` 的左右 7 列）加权求和，得到最终 `data_out`。

两次串联的净效果，等价于用 \(7\times 7\) 的二维核做一次卷积，但运算量从 \(49\) 次乘法降到 \(7+7=14\) 次；又因为核里有 2 个系数为 0（`tap_m2`/`tap_p2`），实际每个 `sharp_arith` 只做 5 次乘法，两次合计仅 \(10\) 次。这就是可分离滤波的效率收益（u2-l1 已在算法层讲过，这里是它的硬件兑现）。

更妙的是：**两个方向共用同一组系数、同一个模块 `sharp_arith`**。所以 `sharp_slice` 例化了两个完全相同的 `sharp_arith`，差别只在喂给它们的抽头一组来自行存储链（垂直）、一组来自移位链（水平）。这种“系数复用”让锐化核的修改只需改一处（`sharp_arith` 的 `sum` 表达式），两个方向自动同步——u6-l3 的二次开发会用到这一点。

一个值得记住的细节：`sharp_arith` 内部对结果做了舍入（`+16`）和饱和截断到 `0..255`（见 [sharp_arith.vhd:34-43](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34-L43)）。这意味着中间结果 `v_out` 已经是一个被舍入、被饱和过的 8 位值，而不是全精度中间量；第二次锐化是在这个“已量化”的中间值上做的。这会引入一点点额外的量化误差，但换来的是两个方向能复用同一个 `sharp_arith`、且中间结果也只有 8 位（省寄存器/省 RAM）。舍入与饱和的完整分析留到 u4-l2。

#### 4.3.2 核心流程

数据从输入到输出，依次经过：

```
data_in
  │
  ▼
① 垂直抽头链: 6× linemem 级联  ──►  v_tap(0..6)   （7 行 × 同列 C）
  │
  ▼
② ver_filt (sharp_arith)        ──►  v_out          （第 R-3 行、列 C 的一维锐化）
  │
  ▼
③ 水平抽头链: 6 级移位寄存器    ──►  h_tap(0..6)   （v_out 的 7 列：C, C-1, ..., C-6）
  │
  ▼
④ hor_filt (sharp_arith)        ──►  data_out       （二维锐化结果）
```

**输出像素落在哪里？** 把两个中心抽头叠在一起：垂直中心是 `v_tap(3)`（第 \(R-3\) 行），水平中心是 `h_tap(3)`（第 \(C-3\) 列）。所以当输入端正在吃第 \((R,C)\) 格像素时，输出端 `data_out` 吐出的是**第 \((R-3,\,C-3)\) 格的二维锐化结果**——即“当前像素往左 3 列、往上 3 行”那一格。整个二维窗口覆盖第 \(R-6\ldots R\) 行、第 \(C-6\ldots C\) 列，共 \(7\times 7\) 格，中心在 \((R-3,C-3)\)。

> 这个“输出滞后输入 3 行 3 列”的空间关系，正是 u3-l2 里“数据通路延迟”的几何来源：行级延迟（约 3 行）来自垂直抽头链里的 3 块行存储，列级延迟（3 列）来自水平抽头链里中心抽头之前的 3 级移位。u3-l2 的 `delay=6` 主要补偿的是流水线寄存器那几拍，而行/列级的空间偏移由 `de` 驱动的逐行扫描结构自然消化。

#### 4.3.3 源码精读

两个 `sharp_arith` 例化结构完全对称，只差抽头来源（`v_tap` 还是 `h_tap`）和输出信号（`v_out` 还是 `data_out`）：

- 垂直滤波器：[FPGA-Design/sharp_slice.vhd:39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49)，7 个 `tap_*` 全部接 `v_tap(0..6)`，输出 `v_out`。
- 水平滤波器：[FPGA-Design/sharp_slice.vhd:60-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70)，7 个 `tap_*` 全部接 `h_tap(0..6)`，输出 `data_out`。

两者例化的是**同一个实体** `work.sharp_arith`，端口映射模式一字不差。这意味着它们用同一套系数。打开 `sharp_arith` 确认系数：[FPGA-Design/sharp_arith.vhd:27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L27) 的注释 `filter coefficients [1;0;-9;48;-9;0;1]/32`，对应的 `sum` 表达式在 [sharp_arith.vhd:34](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)：

```vhdl
sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;
```

逐项对照系数：`tap_m3` 系数 \(1\)、`tap_m2` 系数 \(0\)（**省略不乘**）、`tap_m1` 系数 \(-9\)、`tap_00` 系数 \(48\)、`tap_p1` 系数 \(-9\)、`tap_p2` 系数 \(0\)（**省略不乘**）、`tap_p3` 系数 \(1\)，`+16` 是“加半截断”式四舍五入（u4-l2 详讲），最后 `/32` 即右移 5 位。这与 u2-l2 用 `round(32*fir1(...))` 得到的定点系数完全一致。两个 `sharp_arith` 跑的是同一行代码，所以垂直、水平两个方向天然用同一组系数——这正是“可分离 + 系数对称”带来的硬件简化。

把两次卷积写成显式公式（`+16` 与 `/32` 用 `round_32(·)` 一起表示，饱和到 \(0..255\) 用 `sat(·)` 表示）：

\[
v_{out} = \mathrm{sat}\!\left(\mathrm{round}_{32}\!\left(1\cdot v_{tap}(0) - 9\cdot v_{tap}(2) + 48\cdot v_{tap}(3) - 9\cdot v_{tap}(4) + 1\cdot v_{tap}(6)\right)\right)
\]

\[
data\_out = \mathrm{sat}\!\left(\mathrm{round}_{32}\!\left(1\cdot h_{tap}(0) - 9\cdot h_{tap}(2) + 48\cdot h_{tap}(3) - 9\cdot h_{tap}(4) + 1\cdot h_{tap}(6)\right)\right)
\]

注意 `v_tap(1)`、`v_tap(5)`（系数 0）确实没出现在 \(v_{out}\) 式子里——硬件里它们虽然存在（行存储链照样产出），但 `sharp_arith` 根本不接它们对应的乘法，等于白存。这算是一点点可优化的冗余，但保持链完整、抽头编号整齐，可读性更好。

#### 4.3.4 代码实践（源码阅读 + 推理型）

1. **目标**：确认两次 `sharp_arith` 共用同一组系数，并定位输出像素在原图中的位置。
2. **步骤**：
   - 对比 [sharp_slice.vhd:39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49) 与 [sharp_slice.vhd:60-70](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70)：两处都例化 `work.sharp_arith`，端口映射结构相同，仅抽头来源不同。
   - 打开 [sharp_arith.vhd:27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L27) 与 [sharp_arith.vhd:34](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)，确认系数为 \([1,0,-9,48,-9,0,1]/32\)，且 `tap_m2`/`tap_p2`（系数 0）未出现在 `sum` 中。
3. **推理与预期**：当 `data_in` 正在吃第 \((R,C)\) 格时，`v_tap(3)` 是第 \(R-3\) 行同列，`h_tap(3)` 是 `v_out` 第 \(C-3\) 列；两者中心叠加，`data_out` 输出的是**第 \((R-3,\,C-3)\) 格**的二维锐化结果。若你在仿真里给一张测试图数像素，应该能看到输出图整体相对输入图往右下“滞后”3 行 3 列（边缘几行/几列因窗口不完整而无意义，自校验 testbench 会跳过它们，见 u5-l2）。精确到时钟周期数的实测建议在 u5 的 testbench 中确认。

#### 4.3.5 小练习与答案

1. **问**：把两次一维卷积串联，等价的二维核有多大？中心在哪里？
   **答**：\(7\times 7\)（两个半径 3 的一维核的外积）。中心在两个一维中心的交点，即 `v_tap(3)` 与 `h_tap(3)` 的重叠处，对应原图第 \((R-3,\,C-3)\) 格。

2. **问**：为什么垂直和水平两个 `sharp_arith` 可以共用同一组系数？
   **答**：因为锐化核可分离，且两个方向用的是**同一个**一维核 \(a\)；二维核 \(K_{j,k}=a_j a_k\) 由同一个 \(a\) 派生。硬件上就体现为两个 `sharp_arith` 实例跑同一份系数、同一行 `sum` 代码，只是抽头来源不同。

3. **问**：中间结果 `v_out` 是全精度值吗？这对结果有什么影响？
   **答**：不是。`sharp_arith` 对 `v_out` 做了 `+16`/`/32` 舍入和饱和到 `0..255`，所以第二次锐化建立在一个“已量化、已饱和”的 8 位中间值上，会引入少量额外量化误差。代价是换取两个方向能复用同一个 `sharp_arith`、中间结果只需 8 位存储。细节在 u4-l2。

---

## 5. 综合实践：画出 sharp_slice 的二维抽头数据流图

这是一个贯穿本讲三个模块的任务：在纸上（或文本里）画出 `sharp_slice` 内部 **7 个垂直抽头**与 **7 个水平抽头**的数据流图，标出每个抽头相对中心像素的空间位置。它能把“行存储级联 + 移位寄存器 + 两次 arith”串成一张完整的空间画面。

### 实践目标

用一张图同时表达两件事——**抽头的电路拓扑**（谁连到谁）和**抽头的空间含义**（每个抽头是 7×7 窗口里的哪一格）。

### 操作步骤

1. **画垂直链**（§4.1）：从 `data_in` 出发，画出 6 块 `sharp_linemem` 的级联，标出抽头点 `v_tap(0)..v_tap(6)`，每个抽头旁注明它对应的行号（相对中心 `+3 行 / +2 行 / ... / -3 行`，约定行号向下增长，`v_tap(0)` 最新＝中心下方 3 行）。
2. **画第一次 arith**（§4.3）：7 个 `v_tap` 汇入 `ver_filt`，输出 `v_out`，注明 `v_out` 对应第 \(R-3\) 行、第 \(C\) 列。
3. **画水平链**（§4.2）：`v_out` 进入 6 级移位寄存器，标出抽头点 `h_tap(0)..h_tap(6)`，每个抽头旁注明它对应的列号（相对中心 `+3 列 / ... / -3 列`，`h_tap(0)` 最新＝中心右侧 3 列）。
4. **画第二次 arith**（§4.3）：7 个 `h_tap` 汇入 `hor_filt`，输出 `data_out`，注明它对应第 \((R-3,\,C-3)\) 格。

### 需要观察的现象 / 预期结果

把空间含义单独整理成一张 \(7\times 7\) 窗口表（中心格用 `★` 标出，对应 `data_out` 输出位置；窗口里每个格子的“行列来源”可由对应的 `v_tap(i)` × `h_tap(j)` 组合读出）：

```
              列:  C-6   C-5   C-4   C-3   C-2   C-1    C
                  (h6)  (h5)  (h4)  (h3)★ (h2)  (h1)  (h0)
 行 R-6 (v6)      ·     ·     ·     ·     ·     ·     ·
 行 R-5 (v5)      ·     ·     ·     ·     ·     ·     ·
 行 R-4 (v4)      ·     ·     ·     ·     ·     ·     ·
 行 R-3 (v3)      ·     ·     ·    ★     ·     ·     ·   ← v_out 所在行
 行 R-2 (v2)      ·     ·     ·     ·     ·     ·     ·
 行 R-1 (v1)      ·     ·     ·     ·     ·     ·     ·
 行 R   (v0)      ·     ·     ·     ·     ·     ·     ·   ← data_in 所在行(最新)
```

- 纵向 7 格来自 `v_tap(0..6)`（`v0` 最新＝第 \(R\) 行，在窗口**底部**；`v6` 最旧＝第 \(R-6\) 行，在窗口**顶部**）。
- 横向 7 格来自 `h_tap(0..6)`（`h0` 最新＝第 \(C\) 列，在窗口**最右**；`h6` 最旧＝第 \(C-6\) 列，在窗口**最左**）。
- 中心 `★` = `v_tap(3)` ∩ `h_tap(3)` = 第 \((R-3,\,C-3)\) 格，这就是 `data_out` 当前拍对应的输出像素。
- 当 `data_in` 扫到第 \((R,C)\) 格时，整个 \(7\times 7\) 窗口才刚好“凑齐”，输出端才能给出第 \((R-3,C-3)\) 格的有效锐化结果——所以图像最上方 3 行、最左 3 列没有完整窗口，其输出无意义，自校验 testbench 会跳过（见 u5-l2）。

### 如果想进一步动手

在**自己的副本**上（不要改动课程仓库源码）做下面任一小实验，用 u5 的 testbench 观察：

- 把 `sharp_arith` 的系数从锐化 `[1,0,-9,48,-9,0,1]` 改成恒等 `[0,0,0,32,0,0,0]`（即 `sum := tap_00;`），两次串联后 `data_out` 应≈输入图只是平移了 3 行 3 列——这能验证你对“输出位置在 \((R-3,C-3)\)”的理解。
- 临时把水平移位寄存器改成只有 3 级（`for i in 0 to 2`），观察水平方向“锐化强度”变弱，从而体会抽头数与滤波形状的关系。

## 6. 本讲小结

- `sharp_slice` = **垂直抽头链 + 垂直 arith + 水平抽头链 + 水平 arith**，是把 u2-l1“可分离二维滤波”落到硬件的形态。
- **垂直抽头链**用 6 个 `sharp_linemem` 级联，每级延迟 1 行（1280 有效像素），加上 `v_tap(0)<=data_in` 这根组合连线，凑出 7 个垂直抽头 `v_tap(0..6)`，覆盖同一列的 7 行；中心 `v_tap(3)` = 第 \(R-3\) 行。
- **水平抽头链**用一个 6 级移位寄存器进程，把垂直结果 `v_out` 逐拍右移，凑出 7 个水平抽头 `h_tap(0..6)`，覆盖同一行的 7 列；中心 `h_tap(3)` = 第 \(C-3\) 列。水平方向只需触发器、无需行存储，存储代价远低于垂直方向。
- **两次 `sharp_arith` 串联**等价于一次 \(7\times 7\) 二维卷积，运算量从 49 降到 14（实际 10）；两个方向共用同一组系数 \([1,0,-9,48,-9,0,1]/32\)、同一个模块，所以改系数只需改 `sharp_arith` 一处。
- 输出 `data_out` 对应原图第 \((R-3,\,C-3)\) 格——即“当前输入像素往左 3 列、往上 3 行”；这正是 u3-l2 数据通路延迟的几何来源。
- `generate`（综合期复制硬件）用于垂直行存储链，时钟进程里的 `for`（同一沿并发赋值）用于水平移位链，二者形似但语义不同；中间结果 `v_out` 已被舍入/饱和为 8 位，舍入与饱和细节留 u4-l2。

## 7. 下一步学习建议

- **u4-l1 行存储器 sharp_linemem 的循环缓冲**：本讲只用了“每块 linemem = 延迟 1 行”这一结论，u4-l1 会打开它，精读 1280 项 RAM 的 `wr_address`/`rd_address` 如何循环回绕、`write_en=de_in` 的读写时序。
- **u4-l2 滤波运算 sharp_arith 的定点乘加与饱和截断**：本讲只用“同一组系数、对称 7 抽头”，u4-l2 会精读 `+16` 四舍五入、`/32` 右移、饱和到 `0..255` 的限幅逻辑，并解释 `v_out` 中间量化的影响。
- **u6-l3 架构取舍与二次开发**：本讲建立的“抽头链 + 两次 arith”结构是修改滤波效果的基础——u6-l3 会演示如何改系数、改抽头数，把锐化核换成边缘检测或平滑核，并用自校验 testbench 闭环验证。
- 阅读建议：把本讲的“\(7\times 7\) 窗口表”和 u3-l1 的“像素链路表”、u3-l2 的“延迟对齐分析”三张图对照看，就能把 sharp_slice 的电路拓扑、空间含义、时序延迟三件事一次性打通。
