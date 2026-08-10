# 卷积核实践：高斯模糊与边缘检测

## 1. 本讲目标

前面两讲已经把卷积的「骨架」搭好了：u5-l1 讲清了卷积的数学定义、三个参数位（`clamp`/`source_input`/`add_to_result`）的字节契约、以及「读 → 9 拍乘加 → 写回」三段状态机；u5-l2 讲清了行缓冲如何凑齐 3x3 邻域。**本讲回到主机侧**，专门解决一个工程问题：**我手里只有「位移 + 字节」，怎么亲手构造出一个能实现模糊或边缘检测的卷积核？**

读完本讲，你应该能够：

1. 拿到任一实数卷积核，用 `(n)<<4` / `(n)<<3` 这样的位移把它翻译成本项目能识别的 9 个定点字节。
2. 看懂 `test_gaussian_blur` 的高斯核为什么是 `1-2-1 / 2-4-2 / 1-2-1` 的形状、为什么它「归一化」（字节和 = 16）。
3. 看懂 `test_simple_edge_detection` 里 4 个方向梯度核各自的语义，并理解一个**本项目特有的非显然结论**：因为负梯度响应会被钳到 0，所以必须用 4 个核（而不是 2 个 Sobel 核）才能检出全部方向的边缘。
4. 能复述 `add_to_output` 在累加过程中让 storage 缓冲被「层层叠加」的全过程，并解释为什么卷积结束后必须先 `switch_buffers` 再 `read_image`。
5. 独立在 `main.cpp` 里新增一个 `test_sharpen`（锐化）测试函数。

---

## 2. 前置知识

本讲不再重复卷积的原理与状态机，只把后面要用到的事实列出来（均来自前序讲义）：

- **定点核格式**（u4-l2、u5-l1）：核系数用 1.3.4 定点，**字节值 = 实数 × 16**。所以 `(1)<<4 = 16` 代表 `1.0`，`(1)<<3 = 8` 代表 `0.5`，`(1)<<2 = 4` 代表 `0.25`，`(1)<<1 = 2` 代表 `0.125`，`(1)<<0 = 1` 代表 `0.0625`。负数同理：`(-1)<<4 = -16`，作为 `uint8_t` 是 `0xF0`，硬件加载时符号扩展成 16 位的 `-16`。
- **send_convolution 接口**（u2-l1、u2-l2）：`send_convolution(uint8_t *kernel, bool clamp, bool input_source, bool add_to_output)`。它发出的字节序列是：1 字节操作码 + 1 字节参数位 `(add_to_output<<2)+(input_source<<1)+clamp` + 9 字节核。源码见 [simulation/image_processing_simulation.cpp:203-215](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L203-L215)。
- **读写地址规则**（u3-l2、u3-l4，本讲 4.4 会用到）：卷积结果**恒写回 storage**；`source_input` 决定源图从 input 还是 storage 读；`read_image` 恒从 `buffer_input_address` 读（已确认 [hdl/image_processing.v:239](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L239)）。
- **钳位始终作用于写回**（u5-l1）：卷积写回值经过 `apply_clamp_fixed16(temp_calc, clamp)`（[hdl/image_processing.v:723](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L723)），当 `clamp=true` 时，负的累加结果会被钳到 0。

> 关键术语：卷积核（kernel）、定点位移（fixed-point shift）、归一化（normalization，系数和为 1）、梯度（gradient）、极性（polarity）、Sobel 算子。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 本讲主战场。`test_gaussian_blur`（L93-113）和 `test_simple_edge_detection`（L116-151）两个函数展示了如何构造卷积核；`main` 里的测试选择（L255-256）决定跑哪一个。 |
| [software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | `send_convolution` 纯虚接口（L33），是本讲每个测试函数都要调用的入口。 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | `send_convolution` 的字节打包实现（L203-215），用来确认 9 个核字节就是按 `kernel[0..8]` 顺序发出的。 |
| [build_simulation.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh) / [run_gnuplot.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/run_gnuplot.sh) | 仿真构建与可视化脚本，本讲综合实践用它验证结果。 |

---

## 4. 核心概念与源码讲解

### 4.1 用位移构造定点卷积核：一张速查表

#### 4.1.1 概念说明

本项目没有浮点单元，所以「我想要一个系数 0.25」不能写成 `0.25`，必须写成「能存进一个字节、且数值 = 0.25 × 16 = 4」的定点值。最直接的写法就是位移：`(1)<<2`。掌握本讲的标志，是看到 `conv_kernel[9] = {(1)<<0, (1)<<1, ...}` 这种代码时，能立刻在脑子里换算成实数。

下面这张速查表是本讲最常用的工具，建议背下来：

| 写法 | 字节值（= 实数×16） | 实数 | 典型用途 |
| --- | --- | --- | --- |
| `(1)<<0` | 1 | 0.0625 | 很小的正权重（高斯核四角） |
| `(1)<<1` | 2 | 0.125 | 小正权重（高斯核四边） |
| `(1)<<2` | 4 | 0.25 | 中等权重（高斯核中心） |
| `(1)<<3` | 8 | 0.5 | 半权重（梯度核四角） |
| `(1)<<4` | 16 | 1.0 | 单位权重（梯度核四边 / 恒等核） |
| `(2)<<4` | 32 | 2.0 | 放大权重 |
| `(5)<<4` | 80 | 5.0 | 强放大（锐化核中心） |
| `(-1)<<4` | -16（`0xF0`） | -1.0 | 单位负权重 |
| `(-7)<<4` | -112（`0x90`） | -7.0 | 强负权重 |

设计核的两条总原则：

- **模糊核**：系数全为正，且「实数和 = 1.0」（即**字节和 = 16**），保证对匀强区域不改变亮度——这叫**归一化**。
- **梯度核**：系数有正有负，且「实数和 = 0」（即**字节和 = 0**），保证对匀强区域响应为 0，只对「亮度变化」有反应——这正是边缘检测要的。

> 这两条原则后面会被反复用来验证我们构造的核是否正确。

#### 4.1.2 核心流程

把一个「脑中的实数核」翻译成代码，系统做法是四步：

1. **定语义**：先想清楚这个核要做什么（模糊 / 锐化 / 哪个方向的梯度）。
2. **选精度**：挑最小的位移 `s`，让所有系数都能用 `(n)<<s` 表达且尽量是整数 `n`。本项目常用 `<<4`（精度 1/16，足够细）。
3. **写 9 字节**：按**行优先**（左上→右上→左中→…→右下）填 `kernel[0..8]`。
4. **验和**：把 9 个字节加起来，模糊核应 = 16、梯度核应 = 0。不满足就说明权重没归一化，需要调整。

为什么是「行优先」？因为仿真后端按 `for i in 0..8 push kernel[i]` 发送（[simulation/image_processing_simulation.cpp:208-210](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L208-L210)），硬件则按 `convolution_matrix[8-counter_read]` 反着填，最终 `convolution_matrix[0..8]` 对应核的第 0..8 个字节（u5-l1 已证）。所以 `kernel[0..2]` 是核的上一行、`[3..5]` 是中间行、`[6..8]` 是下一行。

#### 4.1.3 源码精读

`test_gaussian_blur` 里的核（本讲会在 4.2 细讲，这里先看写法）：

- [software/main.cpp:98](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L98)：用 `(1)<<0`、`(1)<<1`、`(1)<<2` 三档位移就拼出了一个完整的高斯核。**没有出现任何小数**，全是整数位移——这就是定点构造法的精髓。

紧接着有一行被注释掉的**备选核**，它已经是一个现成的「边缘增强 / 锐化」例子：

- [software/main.cpp:100](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L100)：

  ```cpp
  uint8_t conv_kernel[9] = {(1)<<4, (1)<<4, (1)<<4, (1)<<4, ((-7)<<4), (1)<<4,
                            (1)<<4, (1)<<4, (1)<<4};
  ```

  它是「8 个 +1.0、中心 -7.0」的核。字节和 = 8×16 + (-7)×16 = 128 - 112 = 16 → 实数和 = 1.0，**归一化**。但因为中心是一个很大的负数，它会把「中心像素」从「8 个邻居之和」里减掉，等价于「放大中心与邻居的差异」——这正是一个锐化核（形状类似 Laplacian）。本项目作者已经把它写好、只是注释掉了，说明「换核」在这个项目里就是改一行字面量这么简单。

#### 4.1.4 代码实践

**实践目标**：建立「实数核 ↔ 定点字节」的双向换算能力。

**操作步骤**：

1. 用 4.1.1 的速查表，把 [software/main.cpp:100](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L100) 备选核的 9 个字节换算成实数，排成 3×3：

   ```
   1.0   1.0   1.0
   1.0  -7.0   1.0
   1.0   1.0   1.0
   ```

2. 验证实数和 = 8×1.0 + (-7.0) = 1.0（归一化 ✓）。
3. 手算一个例子：若 3×3 邻域全为 100，则输出 = 100 × (系数和) = 100 × 1.0 = 100（匀强区域亮度不变 ✓，与 4.1.1 的归一化原则一致）。
4. 再手算一个有边缘的例子：设邻域为 `100 100 100 / 100 200 100 / 100 100 100`（中心突亮），输出 = 8×100×1.0 + 200×(-7.0) + 100×0...（按核加权）= 800 - 1400 + 其余。先算邻居：8 个邻居（其中无中心）权重和 = 8，但这里 8 个邻居都是 100 → 8×100 = 800；中心 200 × (-7) = -1400；总和 = 800 - 1400 = -600。这是一个很大的负数，经 `apply_clamp_fixed16` 取 `[11:4]` 再钳位会被钳到 0。也就是说，这个核会让「比邻居亮很多」的中心点变黑——这正是边缘增强/反锐化的视觉效果。

**需要观察的现象 / 预期结果**：你能不查表地把任意 `(n)<<s` 换算成实数，并能用「字节和」快速判断一个核是归一化核还是零和核。

> 这是纯手算型实践，不需要运行硬件。

#### 4.1.5 小练习与答案

**练习 1**：把「实数 0.375」表示成本项目能用的定点字节。

**答案**：0.375 × 16 = 6，所以字节值 = 6。但 6 不是 2 的整数次幂，没法用 `(1)<<s` 表达，只能直接写 `6`（或 `(3)<<1`）。这提醒我们：定点核不一定非要写成位移形式，写整数字面量 `6` 完全等价，位移只是让「2 的幂次权重」更直观。

**练习 2**：一个核的 9 个字节分别是 `0, 0xF0, 0, 0xF0, 80, 0xF0, 0, 0xF0, 0`（`0xF0` 是 `-16`），它的实数核是什么？归一化吗？

**答案**：`0xF0 = -16 = -1.0`，`80 = 5.0`。实数核为 `0 -1 0 / -1 5 -1 / 0 -1 0`，字节和 = 0×4 + (-16)×4 + 80 = -64 + 80 = 16 → 实数和 1.0，归一化。这就是本讲综合实践要实现的锐化核。

---

### 4.2 高斯模糊核：1-2-1 形状与归一化

#### 4.2.1 概念说明

高斯模糊的核心思想是「加权平均」：离中心越近的像素权重越大、越远越小，权重呈二维高斯钟形分布。最经典的 3×3 近似是 **1-2-1 / 2-4-2 / 1-2-1**：

- 四角（距离 √2）权重最小：1
- 四边（距离 1）权重中等：2
- 中心（距离 0）权重最大：4

为什么是 1:2:4 这个比例？因为它可以看成「行方向的 1-2-1」和「列方向的 1-2-1」两次卷积的乘积（高斯核是**可分离**的），`[1,2,1] ⊗ [1,2,1]^T` 的外积正好得到这个矩阵。这是一个被广泛使用的工程近似。

关键点：**这个核的总和 = 1+2+1+2+4+2+1+2+1 = 16**。在定点世界里，字节和 = 16 意味着实数和 = 16/16 = 1.0，所以它天然归一化——对一片匀强灰度区域，输出 = 输入 × 1.0 = 输入，亮度不变。

#### 4.2.2 核心流程

`test_gaussian_blur`（[software/main.cpp:93-113](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L93-L113)）的流程是一次性卷积：

1. `send_params` + `send_image`：把图送进 input 缓冲。
2. 构造高斯核（字节和 = 16）。
3. `send_convolution(kernel, clamp=true, input_source=true, add_to_output=false)`：从 input 读源图、**覆盖**写回 storage、结果钳位。
4. `wait_end_busy`：等这次卷积跑完。
5. `switch_buffers` + `read_image`：把 storage 里的结果切到 input 侧读出。

注意它**只用了一次**卷积（`add_to_output=false`），因为没有累加需求。

#### 4.2.3 源码精读

高斯核本体（逐字节对应 4.2.1 的 1-2-1/2-4-2/1-2-1）：

- [software/main.cpp:98](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L98)：

  | 位置 | 字节 | 写法 | 实数 |
  | --- | --- | --- | --- |
  | 四角（0,2,6,8） | 1 | `(1)<<0` | 0.0625 |
  | 四边（1,3,5,7） | 2 | `(1)<<1` | 0.125 |
  | 中心（4） | 4 | `(1)<<2` | 0.25 |

  字节和 = 4×1 + 4×2 + 1×4 = 4+8+4 = 16 = 实数 1.0 ✓ 归一化。

调用与收尾：

- [software/main.cpp:102](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L102)：`send_convolution(conv_kernel, true, true, false)`。三个布尔分别是 `clamp=true`（钳位）、`input_source=true`（从 input 读）、`add_to_output=false`（覆盖，不累加）。
- [software/main.cpp:106](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L106)：`wait_end_busy` 等卷积跑完。
- [software/main.cpp:110-112](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L110-L112)：`switch_buffers` 后 `read_image`，把结果读回主机（4.4 会解释为什么必须先 switch）。

#### 4.2.4 代码实践

**实践目标**：通过改核体会「权重分布如何影响模糊强度」。

**操作步骤**：

1. 打开 [software/main.cpp:98](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L98)，把中心从 `(1)<<2`（=4，实数 0.25）改成 `(3)<<2`（=12，实数 0.75），同时把四边四角相应调小，例如改成「四角 0、四边 `(1)<<0`、中心 `(3)<<2`」。

   **示例代码**（仅作改动示意，非项目原代码）：

   ```cpp
   uint8_t conv_kernel[9] = {(0)<<0, (1)<<0, (0)<<0, (1)<<0, ((3)<<2), (1)<<0,
                             (0)<<0, (1)<<0, (0)<<0};
   ```

2. 先验和：4×0 + 4×1 + 1×12 = 16 → 仍归一化 ✓。但权重更集中在中心，模糊会更弱（更接近原图）。
3. 在 `main` 里启用 `test_gaussian_blur`（把 [software/main.cpp:255](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L255) 取消注释、把 L256 注释），用 `build_simulation.sh` 重新构建运行，再用 `run_gnuplot.sh` 看图。

**需要观察的现象 / 预期结果**：权重越集中在中心，输出越接近原图（模糊越弱）；权重越分散（如把四角四边都加大并保持和=16），模糊越强。无论怎么改，只要保持字节和=16，匀强区域的亮度都不变。

> 待本地验证：视觉效果取决于测试图 `image_fruits_64.h`，需 Verilator + gnuplot 环境。无环境时可降级为手算：验证改动后的字节和仍为 16。

#### 4.2.5 小练习与答案

**练习 1**：如果高斯核的 9 个字节全部写成 `(1)<<4`（每个都是 1.0），它还归一化吗？效果是什么？

**答案**：字节和 = 9×16 = 144 → 实数和 = 9.0，远大于 1，**不归一化**。它会把每个像素的亮度放大 9 倍（再被钳到 255），几乎全图饱和变白，不是模糊。

**练习 2**：为什么作者用 `(1)<<0`、`(1)<<1`、`(1)<<2` 三档，而不是直接写 `1, 2, 4`？

**答案**：两者**完全等价**（`(1)<<0`=1、`(1)<<1`=2、`(1)<<2`=4）。用位移写法是为了让读者一眼看出「这是定点格式、权重是 2 的幂次」，呼应核系数的 1.3.4 定点约定。这是可读性选择，不是功能需要。

---

### 4.3 边缘检测：四方向梯度核与「钳位决定要 4 个核」

#### 4.3.1 概念说明

边缘检测用**梯度核**：系数有正有负、和为 0，对「亮度变化剧烈」的位置产生大响应、对匀强区域响应为 0。经典的 Sobel 算子用两个核：`Gx`（水平梯度，检出垂直边）和 `Gy`（垂直梯度，检出水平边），再把两者响应的**绝对值**相加得到边缘强度。

但本项目有一个**硬件约束导致的非显然结论**：卷积写回时 `clamp=true`，`apply_clamp_fixed16` 会把负的累加结果钳到 0（[hdl/image_processing.v:723](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L723)、[hdl/image_processing.v:174-175](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L174-L175)）。也就是说，**一个梯度核只能响应「一个极性」的边缘**：比如「上减下」这个核，当上方更亮时响应为正（保留），当下方更亮时响应为负（被钳到 0，丢失）。

软件世界里你会用 `|Gy|` 取绝对值把两个极性都救回来；但本项目没有「取绝对值」这一步，于是作者的解法是：**为每个方向造两个极性相反的核**——「上减下」和「下减上」各发一次，让两个极性的正响应都被保留，再累加。所以一共 4 个核（上/下/左/右 = 2 个轴向 × 2 个极性）。这是 `test_simple_edge_detection` 用 4 次卷积而非 2 次的根本原因。

每个核的形状沿用 Sobel：**四角权重 0.5（`(±1)<<3`）、四边权重 1.0（`(±1)<<4`）**，比例 0.5:1.0 = 1:2，和标准 Sobel 的 1:2 一致；每个核的字节和都为 0（零和梯度核）。

#### 4.3.2 核心流程

`test_simple_edge_detection`（[software/main.cpp:116-151](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L116-L151)）四次卷积的参数位表（这是本讲最该记住的一张表）：

| 次序 | 核 | 梯度含义 | 检出的边（正响应条件） | `clamp` | `input_source` | `add_to_output` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | top | 上行 − 下行 | 上方更亮的水平边 | true | true | **false**（覆盖初始化） |
| 2 | bottom | 下行 − 上行 | 下方更亮的水平边 | true | true | **true**（累加） |
| 3 | left | 左列 − 右列 | 左侧更亮的垂直边 | true | true | **true**（累加） |
| 4 | right | 右列 − 左列 | 右侧更亮的垂直边 | true | true | **true**（累加） |

两个关键规律：

- **第 1 个核 `add_to_output=false`**：把 storage「初始化」成第一个方向的响应（覆盖掉 storage 里的旧内容）。如果第 1 个也用 `true`，就会把结果叠加到 storage 里的随机垃圾上（u4-l3 提过 storage 初始未清零）。
- **后 3 个核 `add_to_output=true`**：把各自方向的响应叠加到 storage 已有内容上。`source_input` 始终为 `true`——源图永远从 input 读（不变），叠加的是 storage 里**上一轮的累计结果**。

四次之后，storage 里 = 4 个方向的正响应之和，相当于软件里的 `|Gx| + |Gy|`（按极性拆开后求和）。

#### 4.3.3 源码精读

四个方向梯度核（逐个对应 4.3.2 的表）：

- **top gradient**（[software/main.cpp:122](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L122)）：

  ```
   (1)<<3   (1)<<4   (1)<<3       8   16   8      (上行 ×正)
   (0)<<4   (0)<<4   (0)<<4   =   0    0   0
  (-1)<<3  (-1)<<4  (-1)<<3      -8  -16  -8      (下行 ×负)
  ```

  这是「上行 − 下行」，对匀强区域 = 0；上方更亮时为正、下方更亮时为负（被钳到 0）。

- **bottom gradient**（[software/main.cpp:129](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L129)）：top 的**整体取反**，检出「下方更亮」的极性。

- **left gradient**（[software/main.cpp:136](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L136)）：「左列 − 右列」，检出垂直边（左侧更亮的极性）。

- **right gradient**（[software/main.cpp:143](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L143)）：left 的整体取反，检出「右侧更亮」的极性。

注意每个核里**角邻居用 `(±1)<<3`（0.5）、边邻居用 `(±1)<<4`（1.0）**：这正是 Sobel「直接邻居权重 2、对角邻居权重 1」的比例（这里整体缩小一半，但比例 1:2 不变，且字节和仍为 0）。

四次调用与 `add_to_output` 的差异：

- 第 1 次 top：`send_convolution(conv_kernel, true, true, false)`（[software/main.cpp:123](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L123)）——第四个参数 `false`，覆盖写回。
- 第 2/3/4 次（[software/main.cpp:130](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L130)、[software/main.cpp:137](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L137)、[software/main.cpp:144](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L144)）：第四个参数 `true`，叠加写回。每次后都 `wait_end_busy`（L125、L132、L139、L146）。

#### 4.3.4 代码实践

**实践目标**：追踪 storage 缓冲在四次卷积中如何被「层层叠加」，并验证「平坦区域响应为 0」。

**操作步骤**：

1. 把 storage 当成一个寄存器，按下表逐步演算它在本应存放「某像素位置结果」的那个单元里的值（设源图该位置附近的三种典型情况）：

   | 阶段 | storage 内容（某像素） | 说明 |
   | --- | --- | --- |
   | 初始 | 0（或未定义） | `add_to_output=false` 会覆盖，故初值无所谓 |
   | after top（覆盖） | `max(top_resp, 0)` | top 响应，负则钳 0 |
   | after bottom（累加） | `prev + max(bottom_resp, 0)` | 叠加 bottom 的正响应 |
   | after left（累加） | `prev + max(left_resp, 0)` | 叠加 left |
   | after right（累加） | `prev + max(right_resp, 0)` | 叠加 right |

2. **平坦区域验证**：设邻域 9 个像素全相同（如全 100）。四个核的字节和都是 0，故每个梯度响应 = 100 × 0 = 0，四次累加后 storage = 0。这对应「平坦区域接近黑」的预期 ✓。
3. **强边缘验证**：设邻域为「上半 255、下半 0」（一条水平边）。top 核 = 255×(8+16+8) + 0×(-8-16-8) = 255×32 = 8160；经 `apply_clamp_fixed16` 取 `[11:4]` = 8160/16 = 510，再钳位到 255。bottom 核在该处为负（下方暗）→ 钳到 0。left/right 核对水平边响应很小。所以 storage ≈ 255（亮线）✓。

**需要观察的现象 / 预期结果**：你能说出「每条边会被 4 个核里的 1~2 个以正响应检出，并累加成亮线；平坦区域为 0」。

> 这是手算 + 推理型实践，不需运行硬件。结合 4.4.4 可在仿真里看到真实图像的效果。

#### 4.3.5 小练习与答案

**练习 1**：如果只用 top 和 left 两个核（省掉 bottom 和 right），会漏检什么样的边？

**答案**：会漏检「下方更亮」的水平边（bottom 负责的极性，top 在那里响应为负被钳到 0）和「右侧更亮」的垂直边（right 负责的极性）。图像里这些位置的轮廓会消失。

**练习 2**：为什么梯度核的角邻居用 `(±1)<<3`（0.5）而不是和边邻居一样用 `(±1)<<4`（1.0）？

**答案**：因为对角邻居离中心更远（距离 √2 vs 1），对中心梯度的贡献应更小。Sobel 给对角邻居的权重是直接邻居的一半（1 vs 2），这里用 0.5 vs 1.0 复现了这个 1:2 比例。若四角也用 1.0，就成了无加权的简单差分核，对各方向噪声更敏感、定位精度也更差。

---

### 4.4 add_to_output 累加写回与 switch_buffers → read_image 收尾

#### 4.4.1 概念说明

`add_to_output`（硬件寄存器 `convolution_add_to_result`）在 u5-l1 已讲过机制，本讲把它落到「storage 缓冲的内容如何一步步演化」上，并补上最后一步「为什么必须 switch_buffers 再 read_image」。

**覆盖 vs 叠加**：

- `add_to_output=false`：写回值 = 卷积结果（忽略 storage 原值）。等号右边 `convolution_data_to_add` 被置 0（[hdl/image_processing.v:647-649](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L647-L649)）。
- `add_to_output=true`：写回前先用两拍读出 storage 写地址处的**原值**进 `convolution_data_to_add`（[hdl/image_processing.v:633-641](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L633-L641)），写回值 = 原值 + 卷积结果（[hdl/image_processing.v:725](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L725)）。

**为什么最后要 `switch_buffers` 再 `read_image`**：这是双缓冲模型的直接推论。`send_image`（写图）和 `read_image`（读图）都恒从 `buffer_input_address` 计址（[hdl/image_processing.v:234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L234) 与 [hdl/image_processing.v:239](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L239)），而卷积结果恒写回 storage。所以「图在 input、结果在 storage」。要把结果读回主机，就得先用 `COMMAND_SWITCH_BUFFERS` 把两个地址寄存器**互换**（[hdl/image_processing.v:255-256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L255-L256)），让 `buffer_input_address` 指向原 storage 区，`read_image` 才能读到结果。这只是改两个地址寄存器、不搬数据（零拷贝，详见 u3-l2）。

#### 4.4.2 核心流程

`test_simple_edge_detection` 收尾的 storage 演化与读回：

1. 四次卷积后，storage = 4 个方向响应之和（见 4.3.2）。
2. `switch_buffers`（[software/main.cpp:148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L148)）：`buffer_input_address` ↔ `buffer_storage_address` 互换。现在 input 指针指向「4 方向累加结果」，storage 指针指向「原始输入图」。
3. `read_image`（[software/main.cpp:150](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L150)）：从 input 侧把结果逐字节读回主机 `image_output` 数组。

对比 `test_gaussian_blur`：它只有一次卷积，但收尾完全一样（[software/main.cpp:110-112](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L110-L112)）——`switch_buffers` 再 `read_image`。这是所有卷积测试的统一套路。

#### 4.4.3 源码精读

收尾两行：

- [software/main.cpp:148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L148)：`img_proc->switch_buffers();`
- [software/main.cpp:150](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L150)：`img_proc->read_image(image_output);`

它们对应的硬件动作：

- `switch_buffers` → `COMMAND_SWITCH_BUFFERS`：两条非阻塞赋值在一个时钟沿把两个地址寄存器互换（[hdl/image_processing.v:255-256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L255-L256)）。
- `read_image` → `COMMAND_READ_IMG`：`memory_addr_counter` 初始化为 `buffer_input_address`（[hdl/image_processing.v:239](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L239)），然后逐字读出（读取的内部两级流水见 u3-l4）。

#### 4.4.4 代码实践（可运行）

**实践目标**：在仿真里把边缘检测和高斯模糊都跑出来，对照本讲的预测。

**操作步骤**：

1. 确认 [software/main.cpp:256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L256) 当前激活的是 `test_simple_edge_detection`（其余 `test_*` 已注释）。
2. 按 u1-l3 的方式：执行 `build_simulation.sh`，再运行生成的 `./simu`，它会写出 `output.dat`。
3. 用 `run_gnuplot.sh` 渲染灰度图查看边缘检测结果。
4. 把 L256 注释、取消注释 L255 启用 `test_gaussian_blur`，重复构建运行，对比两张输出图。

**需要观察的现象 / 预期结果**：

- 高斯模糊：输出更平滑、细节减弱，但整体亮度接近原图（核归一化和 = 1.0）。
- 边缘检测：物体轮廓处出现亮线，平坦区域接近 0；4 个方向叠加后，响应强的边缘会偏亮甚至饱和到 255。

> 待本地验证：视觉结果取决于测试图 `image_fruits_64.h` 的内容，需 Verilator + gnuplot 环境。无环境时降级为 4.3.4 的手算验证。

#### 4.4.5 小练习与答案

**练习 1**：如果忘了在 `read_image` 之前调用 `switch_buffers`，会读到什么？

**答案**：会读到 input 缓冲里的**原始输入图**（因为 `read_image` 恒从 `buffer_input_address` 读，而未经 switch 时 input 指针还指向原图）。卷积结果在 storage 里、读不到，输出的就是未经处理的原图。

**练习 2**：`test_simple_edge_detection` 第 1 个核用 `add_to_output=false`，如果误写成 `true` 会怎样？

**答案**：第 1 次卷积会把 top 响应叠加到 storage 写地址处的**未初始化旧值**上。storage 初始内容未定义，最终结果会被这些垃圾值污染，边缘图上会出现随机亮斑/噪声。

---

## 5. 综合实践：新增一个 `test_sharpen`（锐化）

把本讲全部内容串起来，完成一个贯穿性的动手任务：在 `main.cpp` 里新增一个锐化测试函数。这个任务覆盖「定点核构造（4.1）→ 归一化验证（4.2）→ 单次卷积覆盖写回（4.3 的第 1 次调用同款用法）→ switch_buffers + read_image 收尾（4.4）」全链路。

### 任务要求

构造一个锐化核：**中心 +5、上下左右四邻 -1、四角 0**，用 `(n)<<4` 定点表示，先发一次卷积（`add_to_output=false`）得到锐化结果，最后 `switch_buffers` + `read_image` 输出。

### 第 1 步：构造并验证核

锐化核的实数形式：

```
 0  -1   0
-1   5  -1
 0  -1   0
```

转成定点字节（每个 × 16，负数用 `uint8_t` 表示）：

| 位置 | 实数 | 写法 | 字节 |
| --- | --- | --- | --- |
| 四角 | 0 | `(0)<<4` | 0 |
| 四邻 | -1 | `(-1)<<4` | -16（`0xF0`） |
| 中心 | 5 | `(5)<<4` | 80 |

字节和 = 4×0 + 4×(-16) + 80 = -64 + 80 = 16 → 实数和 1.0，**归一化** ✓（匀强区域亮度不变；中心与邻居差异被放大 → 锐化）。

### 第 2 步：写出函数（示例代码）

仿照 `test_gaussian_blur`（[software/main.cpp:93-113](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L93-L113)）的结构。下面是参考实现，**标注为示例代码**（非项目原有代码）：

```cpp
// 示例代码：锐化卷积测试
void test_sharpen(uint8_t *image_input, uint8_t *image_output, Image_processing *img_proc){
   img_proc->send_params(image_width, image_height);
   img_proc->send_image(image_input);

   // sharpen kernel: center +5, 4-neighbours -1, corners 0 (1.3.4 fixed point)
   uint8_t conv_kernel[9] = {(0)<<4,  (-1)<<4, (0)<<4,
                             (-1)<<4, (5)<<4,  (-1)<<4,
                             (0)<<4,  (-1)<<4, (0)<<4};

   // 从 input 读、覆盖写回 storage、钳位
   img_proc->send_convolution(conv_kernel, true, true, false);

   img_proc->wait_end_busy();

   img_proc->switch_buffers();
   img_proc->read_image(image_output);
}
```

### 第 3 步：挂到 main 的测试选择并验证

1. 把上面函数粘到 `main.cpp` 里（例如紧跟 `test_gaussian_blur` 之后）。
2. 在 `main` 的测试选择区（[software/main.cpp:252-260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L252-L260)），把其它 `test_*` 注释、加上一行 `test_sharpen(image_input, image_output, img_proc);`。
3. 执行 `build_simulation.sh` 重新构建，运行 `./simu` 生成 `output.dat`。
4. 用 `run_gnuplot.sh` 查看锐化效果。

### 预期结果与现象

- 对匀强区域：输出 = 输入（核归一化和 = 1.0）。
- 对边缘处：中心与邻居的差异被放大，轮廓更清晰、对比度更高（锐化）。
- 若发现整体变亮或变暗，先检查字节和是否仍 = 16（归一化是否被破坏）。

> 待本地验证：锐化强度与测试图内容相关，需 Verilator + gnuplot 环境。无环境时，至少完成第 1 步的字节构造与归一化手算验证，并口述「为什么这个核能锐化」。

---

## 6. 本讲小结

- 定点核的构造本质是「实数 × 16 存成字节」，`(1)<<4`=1.0、`(1)<<3`=0.5、`(1)<<2`=0.25；负数用 `uint8_t` 表示并由硬件符号扩展。一张位移速查表（4.1.1）能覆盖绝大多数核。
- 两条设计原则：**模糊核字节和 = 16（归一化）**，**梯度核字节和 = 0（零和）**。`test_gaussian_blur` 的 `1-2-1/2-4-2/1-2-1` 高斯核字节和正好 16，故匀强区域亮度不变。
- 边缘检测的**非显然结论**：因为 `clamp=true` 会把负梯度钳到 0（[hdl/image_processing.v:723](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L723)），一个梯度核只响应一个极性，所以 `test_simple_edge_detection` 必须用 **4 个核**（上/下/左/右 = 2 轴 × 2 极性）而不是 2 个 Sobel 核。
- 多核累加靠 `add_to_output`：第 1 个核 `false`（覆盖初始化 storage），后续核 `true`（叠加）；`source_input` 始终 `true` 让源图不变、结果层层叠加进 storage。
- 收尾统一套路：**`switch_buffers` + `read_image`**。因为卷积结果恒写回 storage、而 `read_image` 恒从 input 读（[hdl/image_processing.v:239](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L239)），必须先用 `switch_buffers` 互换地址（[hdl/image_processing.v:255-256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L255-L256)）才能把结果读回主机。

---

## 7. 下一步学习建议

到这里，「3x3 卷积引擎」单元的三篇讲义就闭环了：u5-l1 讲骨架与契约、u5-l2 讲数据通路、本讲讲主机侧的核构造与上机实践。接下来可以：

1. **向「两条后端」深入**：本讲所有调用都经过 `send_convolution` 这个抽象接口。后续 u6-l1（Verilator 仿真后端）会讲这些字节是如何入队、如何驱动 `Vimage_processing` 模型的；u6-l3/u6-l4 会讲它们在硬件上如何经 SPI 送到 FPGA。结合本讲你会看到「同一个核构造、两条物理通路」的完整图景。
2. **尝试更复杂的核链**：仿照 `test_simple_edge_detection` 的多核累加，设计「先高斯模糊去噪、再边缘检测」的两阶段流水（注意 `source_input` 与缓冲切换的配合），检验你对 `add_to_output` + `switch_buffers` 的理解。
3. **回看 u7-l2（扩展新运算）**：本讲的 `test_sharpen` 只动了主机侧；如果你想新增一个**全新的命令**（而非新核），就要同时改接口、两套后端和 HDL——那是 u7-l2 的主题，本讲是它的前置练习。
