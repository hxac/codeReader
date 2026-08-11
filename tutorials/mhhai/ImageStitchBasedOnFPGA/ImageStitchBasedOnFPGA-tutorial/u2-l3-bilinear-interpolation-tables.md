# 双线性插值与地址/权重表生成

> 本讲属于「进阶层·圆柱面投影算法与软件参考实现」单元，承接 [u2-l2 圆柱面投影的数学原理](u2-l2-cylindrical-projection-math.md)。
> 上一讲我们得到了每个目标像素对应的**浮点源坐标**（`xmap`/`ymap`）；本讲要解决的问题是：浮点坐标无法直接当数组下标用，怎样把它变成「去哪几个像素取值」和「各取多大比例」这两张表，并最终交给 FPGA 当查表系数。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚**双线性插值（bilinear interpolation）**为什么要用 4 个相邻像素、4 个权重，以及权重为何要满足「求和为 1」。
2. 读懂 `圆柱面投影.cpp` 中 `warp` 函数手写的 **addr 地址表**（`col*8`）与 **weight 权重表**（`col*4`）的内存组织方式，理解「每个目标像素占 8 个 int / 4 个 float」这种紧凑排列。
3. 把 C++ 的 4 个地址对，准确对应到 `圆柱面投影.v` 里的 `weight_x00/weight_y00 … weight_x11/weight_y11` 硬件信号，看懂「软件表 → 硬件查表」的交接点。
4. 理解 `.coe` 文件在本项目中的真实写法（OpenCV `FileStorage` 序列化），并知道它和 Xilinx 原生 `.coe` 格式的差别。
5. 具备**批判性阅读**真实代码的能力——能发现并验证源码里权重表的一处错位。

## 2. 前置知识

在进入源码前，先用三段通俗的话把背景补齐。

### 2.1 为什么需要插值

图像变换的本质是一个数学映射：把「目标图上的整数像素 `(u,v)`」通过反向映射函数算回「源图上的某个位置 `(x,y)`」。问题在于，这个 `(x,y)` 几乎总是**带小数**的，比如 `(3.7, 5.2)`。而源图只在整数坐标 `(3,5)`、`(4,5)`、`(3,6)`、`(4,6)` 这些格点上存有真实像素值。带小数的位置没有现成像素可用，必须根据周围格点「估算」出一个值，这个估算过程就叫**插值**。

- **最近邻插值**：四舍五入取最近的那个格点，简单但画面会有锯齿。
- **双线性插值**：用周围 4 个格点按距离加权平均，画面平滑，是 OpenCV `INTER_LINEAR` 的做法，也是本讲主角。

> 回忆 [u1-l1](u1-l1-project-overview.md)：README 明确提到双线性插值「需要在一个时钟周期取出四个像素」，资源占用大；最近邻「简单实用，工程推荐」。本讲讲的是双线性这条「质量好但费资源」的路线，理解了它，你才能体会 README 在资源与质量之间做取舍的难度。

### 2.2 取整函数 cvFloor 与 cvCeil

源码大量使用 OpenCV 的两个取整函数，先约定好含义（注意是朝正负无穷方向，不是「四舍五入」）：

| 函数 | 含义 | 例子 |
|------|------|------|
| `cvFloor(x)` | 向 \(-\infty\) 取整（下取整） | `cvFloor(3.7)=3`，`cvFloor(-11.4)=-12` |
| `cvCeil(x)` | 向 \(+\infty\) 取整（上取整） | `cvCeil(3.2)=4`，`cvCeil(-11.4)=-11` |

源码第 175–178 行专门打了 4 行调试输出，就是为了让作者自己确认这两个函数对负数的行为（注意 `cvFloor(-11.4)` 打印的是 `-12` 而非 `-11`，这一点对带负坐标的圆柱面投影非常关键）：[圆柱面投影.cpp:L175-L178](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L175-L178)。

给定一个浮点坐标 `x`，它的左右两个相邻格点就是 `x1 = cvFloor(x)` 与 `x2 = cvCeil(x)`。当 `x` 本身是整数时 `x1 == x2`，插值退化为直接取值。

### 2.3 从上一讲继承的两张浮点表

[u2-l2](u2-l2-cylindrical-projection-math.md) 里 `buildMaps` 通过反向映射 `mapBackward` 为每个目标像素算出了源坐标，存成两张浮点图：

- `xmap`（代码里别名 `a`）：每个目标像素对应的**源列坐标** \(x\)；
- `ymap`（代码里别名 `b`）：每个目标像素对应的**源行坐标** \(y\)。

注意命名陷阱：变量 `a` 存的是 **x（列）**，`b` 存的是 **y（行）**，和字母顺序相反。本讲的所有公式都要盯紧这个对应关系，否则极易读反。

## 3. 本讲源码地图

本讲只精读一个文件，但会对照它的硬件孪生：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [圆柱面投影.cpp](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp) | OpenCV 软件参考实现 | `warp` 函数（L148–L266）：手写双线性、生成 addr/weight 表、导出 `.coe` |
| [圆柱面投影.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.v) | 硬件定点实现 | `weight_x00 … weight_x11` 等信号（L26–L27、L48–L55、L93–L100）：与 C++ 地址表的逐项对应 |

整个 `warp` 函数的内部脉络可以画成一条流水线：

```
buildMaps() ──► xmap/ymap（浮点源坐标，来自 u2-l2）
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
   addr 地址表                    weight 权重表
 (每像素 8 个 int)              (每像素 4 个 float)
        │                             │
        └──────────────┬──────────────┘
                       ▼
            res（手写双线性结果，存 res.bmp）
                       │
                       ▼
        FileStorage 导出 addr.coe（L258-L259）
                       │
                       ▼
   remap()（OpenCV 自带双线性，真正返回给流水线，L262）
```

记住一个关键事实：**手写的 `res` 只是「参考答案 + 抽系数」，并没有真正返回给拼接流水线**——第 261 行 `//res.copyTo(dst);` 被注释掉了，函数最后用的是 OpenCV 自带的 `remap`（第 262 行）。所以 `addr/weight/res` 这一整块代码的真正使命是：验证作者自己理解的双线性是对的，并把地址表导出给 FPGA。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**warp 全景 → addr 地址表 → weight 权重表 → .coe 导出与硬件交接**。

### 4.1 warp 函数全景：从浮点映射到查表

#### 4.1.1 概念说明

`warp` 是「把源图按相机参数投影变换成目标图」的总入口。上一讲我们已经知道，`buildMaps` 产出了「目标像素 → 源坐标」的浮点映射；`warp` 要在这之上完成两件事：

1. **取值**：根据浮点源坐标，找到源图上 4 个相邻的整数像素；
2. **加权**：用双线性权重把这 4 个像素融合成 1 个目标像素。

但作者并没有止步于「算出结果图」。他额外维护了 `addr`、`weight` 两张表，把中间过程显式存下来。这样做有两个目的：第一，可以离线检查每个像素到底取了哪 4 个点、各占多少比例；第二，这两张表可以直接导出成 FPGA 的查表系数。换句话说，`warp` 同时承担了「图像变换器」和「系数生成器」两个角色。

#### 4.1.2 核心流程

`warp` 的执行步骤如下：

1. 调 `buildMaps` 得到 `xmap`/`ymap`（浮点源坐标表），并算出目标区域 `dst_roi`。
2. 把 `xmap`/`ymap` 复制成 `a`/`b`，同时存成 `xmap.bmp`/`ymap.bmp` 便于肉眼查看坐标分布。
3. 创建三张表：`addr`（地址表）、`weight`（权重表）、`res`（手写结果图）。
4. 双重循环遍历每个目标像素，分别填充 `addr`、`weight`。
5. 再一次双重循环，用 `addr`+`weight`+源图 `s` 算出 `res`，并对越界地址做置零保护。
6. 把 `res` 存成 `res.bmp`，把 `addr` 存成 `addr.coe`。
7. 最后调用 OpenCV 自带的 `remap` 得到「官方」结果并返回（手写结果被注释掉）。

#### 4.1.3 源码精读

先看 `warp` 的签名和建表部分：[圆柱面投影.cpp:L148-L169](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L148-L169)。

```cpp
Point warp(InputArray src, InputArray K, InputArray R, int interp_mode, int border_mode,
           OutputArray dst)
{
    UMat xmap, ymap;
    ...
    Rect dst_roi = buildMaps(src.size(), K, R, xmap, ymap);   // 上一讲的产物
    dst.create(dst_roi.height + 1, dst_roi.width + 1, src.type());
    Mat a, b, c, d;
    xmap.copyTo(a);   // a = xmap = 源「列」坐标 x
    ymap.copyTo(b);   // b = ymap = 源「行」坐标 y
    ...
    Mat addr, weight, res;
    addr.create(1100, 1086 * 8, CV_32SC1);    // 每像素 8 个 int
    weight.create(1100, 1086 * 4, CV_32FC1);  // 每像素 4 个 float
    res.create(1100, 1086, CV_8UC3);          // 三通道 8bit 结果
```

要点有三：

- **`a` 存 x，`b` 存 y**（与字母顺序相反，再次强调）。
- **尺寸被写死成 `1100 × 1086`**：这是作者那组特定采集图（IFOV 11.bmp/22.bmp）经 `detectResultRoi` 算出来的目标图尺寸。换一组图就得改这三个魔法数，代码并不通用。这一点在 [u1-l2](u1-l2-repo-navigation.md) 评价「这是一份源码片段集」时已埋下伏笔。
- **`addr` 每像素占 8 列、`weight` 每像素占 4 列**：这是一种「每个目标像素一条记录」的紧凑排列（详见 4.2）。

再看结果合成与收尾：[圆柱面投影.cpp:L208-L266](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L208-L266)。其中第 216–231 行是**越界保护**：当目标像素落在画面边缘（`row<12` 或 `row>1080`）且反向映射出的源地址出现了负数或超过 1100 时，直接把目标像素置黑，避免数组越界访问。第 257–259 行存图与导出 coe，第 261–262 行的关键对比：

```cpp
imwrite("res.bmp", res);
FileStorage fs("addr.coe", FileStorage::WRITE);
fs << "addr" << addr;

//res.copyTo(dst);                          // 手写结果被注释
remap(src, dst, xmap, ymap, interp_mode, border_mode);  // 实际返回 OpenCV 结果
```

这证实了我们的判断：`addr/weight/res` 是「参考实现 + 抽系数」，`remap` 才是真正交回流水线的结果。

#### 4.1.4 代码实践

**实践目标**：确认 `a`/`b` 与 `x`/`y` 的对应关系，避免后续读表时读反。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [圆柱面投影.cpp:L159-L160](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L159-L160)，确认 `xmap.copyTo(a)`、`ymap.copyTo(b)`。
2. 再回到上一讲 [buildMaps](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L141-L142)（L141–L142），确认 `xmap` 写入的是 `mapBackward` 的输出参数 `x`（列），`ymap` 写入的是 `y`（行）。
3. 在笔记本上写下结论：**`a[row][col]` = 源列号 x；`b[row][col]` = 源行号 y**。

**预期结果**：你能不假思索地说出「`a` 是列、`b` 是行」。这是后续两节不读反的前提。

#### 4.1.5 小练习与答案

**练习 1**：`warp` 函数最后为什么用 `remap` 而不是返回手写的 `res`？

> **参考答案**：因为手写的 `res`（连同 `addr`/`weight`）主要是为了**验证作者对双线性的理解**并**导出 FPGA 查表系数**，`res.copyTo(dst)` 被注释掉了；让 OpenCV 官方的 `remap` 产出的结果更可靠，作为拼接流水线后续阶段的输入。

**练习 2**：三张表的尺寸 `1100`、`1086` 是从哪来的？换成不同分辨率的图会怎样？

> **参考答案**：来自作者那组特定图经圆柱面投影后的目标区域尺寸（与 `dst_roi` 对应）。它们被硬编码，换图后必须同步修改 `addr/weight/res` 的 `create` 尺寸，否则会越界或浪费内存——这正是这份代码「片段化、非通用」的体现。

---

### 4.2 addr 地址表：定位四个相邻源像素

#### 4.2.1 概念说明

要把浮点源坐标 `(x,y)` 转成「可读的源图像素」，最直接的想法是取它周围的 4 个整数格点：

- 左上：\((x_1, y_1) = (\text{cvFloor}(x),\ \text{cvFloor}(y))\)
- 左下：\((x_1, y_2) = (\text{cvFloor}(x),\ \text{cvCeil}(y))\)
- 右上：\((x_2, y_1) = (\text{cvCeil}(x),\ \text{cvFloor}(y))\)
- 右下：\((x_2, y_2) = (\text{cvCeil}(x),\ \text{cvCeil}(y))\)

其中 \(x_1, x_2\) 是列号，\(y_1, y_2\) 是行号。**addr 地址表**就是把每个目标像素对应的这 4 个格点的 `(行, 列)` 坐标全部预存下来，这样后续插值（以及 FPGA）只需查表就能知道去源图哪 4 个位置取像素，不必再实时做 `cvFloor/cvCeil`。

#### 4.2.2 核心流程

addr 表是一个 `1100` 行、`1086*8 = 8688` 列的 `int` 矩阵（`CV_32SC1`）。可以把每一行理解成一条「扫描线」，里面**连续 8 个 int 描述 1 个目标像素的 4 个源地址**：

```
目标像素 col 的 4 个源地址，占据 addr[row][col*8 .. col*8+7]：

  偏移 0,1 : (y_floor, x_floor)   ← 左上
  偏移 2,3 : (y_ceil , x_floor)   ← 左下
  偏移 4,5 : (y_floor, x_ceil )   ← 右上
  偏移 6,7 : (y_ceil , x_ceil )   ← 右下
```

每个地址都是「先存行（y），再存列（x）」，这与 OpenCV `Mat::at<Vec3b>(row, col)` 的下标顺序一致——稍后在合成结果时，正是用 `addr[8i]` 当 row、`addr[8i+1]` 当 col 去索引源图。

> 这种「每像素一条定长记录、紧密排列」的布局，本质上是为 FPGA 查表服务的：把 `addr` 一行行灌进 Block RAM，目标像素序号 `col` 乘 8 就是基地址，连续读 8 个字就能拿到 4 个源地址，非常适合硬件的顺序突发读。

#### 4.2.3 源码精读

地址表生成的双重循环：[圆柱面投影.cpp:L180-L193](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L180-L193)。

```cpp
for (int row = 0; row < xmap.rows; ++row)
    for (int col = 0; col < xmap.cols; ++col)
    {
        addr.at<int>(row, col * 8)     = cvFloor(b.at<float>(row, col)); // y_floor
        addr.at<int>(row, col * 8 + 1) = cvFloor(a.at<float>(row, col)); // x_floor
        addr.at<int>(row, col * 8 + 2) = cvCeil (b.at<float>(row, col)); // y_ceil
        addr.at<int>(row, col * 8 + 3) = cvFloor(a.at<float>(row, col)); // x_floor
        addr.at<int>(row, col * 8 + 4) = cvFloor(b.at<float>(row, col)); // y_floor
        addr.at<int>(row, col * 8 + 5) = cvCeil (a.at<float>(row, col)); // x_ceil
        addr.at<int>(row, col * 8 + 6) = cvCeil (b.at<float>(row, col)); // y_ceil
        addr.at<int>(row, col * 8 + 7) = cvCeil (a.at<float>(row, col)); // x_ceil
    }
```

逐行解读（牢记 `a`=列 x，`b`=行 y）：

- 偏移 0、1：`cvFloor(b)`、`cvFloor(a)` → 行下取整、列下取整 → **左上 \((x_1,y_1)\)**。
- 偏移 2、3：`cvCeil(b)`、`cvFloor(a)` → 行上取整、列下取整 → **左下 \((x_1,y_2)\)**。
- 偏移 4、5：`cvFloor(b)`、`cvCeil(a)` → 行下取整、列上取整 → **右上 \((x_2,y_1)\)**。
- 偏移 6、7：`cvCeil(b)`、`cvCeil(a)` → 行上取整、列上取整 → **右下 \((x_2,y_2)\)**。

注意 4 个 `x_floor`/`x_ceil` 里只有两种不同的列值（\(x_1\) 与 \(x_2\)），4 个行值也只有两种（\(y_1\) 与 \(y_2\)），两两组合成 4 个角。代码之所以展开成 8 个赋值，是为了让后续的 `res` 合成循环能用统一的「`addr[8i+k]`」方式顺序读取，避免在内存里再做组合。

随后在结果合成里，这 8 个 int 被成对当作 `(row, col)` 去源图 `s` 取像素，见 [圆柱面投影.cpp:L239-L243](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L239-L243)（仅看通道 0 的写法）：

```cpp
res.at<Vec3b>(row, col)[0] =
    weight.at<float>(row, col*4)   * s.at<Vec3b>( addr.at<int>(row, col*8),   addr.at<int>(row, col*8+1) )[0] +
    weight.at<float>(row, col*4+1) * s.at<Vec3b>( addr.at<int>(row, col*8+2), addr.at<int>(row, col*8+3) )[0] +
    weight.at<float>(row, col*4+2) * s.at<Vec3b>( addr.at<int>(row, col*8+4), addr.at<int>(row, col*8+5) )[0] +
    weight.at<float>(row, col*4+3) * s.at<Vec3b>( addr.at<int>(row, col*8+6), addr.at<int>(row, col*8+7) )[0];
```

这段代码把「addr 决定去哪取、weight 决定各占多少」体现得淋漓尽致：4 项分别对应左上、左下、右上、右下 4 个源像素。

#### 4.2.4 代码实践

**实践目标**：用伪代码重写地址表生成逻辑，确认你理解了「8 个 int = 4 个 (行,列) 对」。

**操作步骤**：

1. 先在草稿纸上写出两个中间量：
   ```
   y1 = floor(b[row][col]);  y2 = ceil(b[row][col]);   // 行：下/上
   x1 = floor(a[row][col]);  x2 = ceil(a[row][col]);   // 列：下/上
   ```
2. 把上面 8 条赋值改写成等价的「4 个坐标对」伪代码：
   ```
   pixel0 = (y1, x1)  // 左上
   pixel1 = (y2, x1)  // 左下
   pixel2 = (y1, x2)  // 右上
   pixel3 = (y2, x2)  // 右下
   ```
3. 对照第 240–243 行的合成式，确认 `pixel0..pixel3` 的顺序与 `weight[col*4 .. col*4+3]` 一一对应。

**需要观察的现象 / 预期结果**：你会发现，地址表本质就是把 \((x_1,x_2,y_1,y_2)\) 这 4 个值（在非整数点处）组合成 4 个角；当 `x` 或 `y` 为整数时 \(x_1=x_2\) 或 \(y_1=y_2\)，4 个角会退化成 2 个甚至 1 个，对应的 weight 也会自动归零（见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么地址表用 `CV_32SC1`（32 位有符号整型）而不是无符号整型？

> **参考答案**：因为圆柱面投影后目标区域 `dst_tl` 可能是**负坐标**（见 [u2-l2](u2-l2-cylindrical-projection-math.md) 的 `detectResultRoi`），反向映射回源图的坐标也可能出现负数（第 177 行 `cvFloor(b)= -11` 即为例证）。有符号整型才能正确表达这些负地址，便于后续越界判负（第 216 行 `addr < 0`）。

**练习 2**：把「每个像素 8 个 int」改成「每个像素 1 个 `struct{int y0,x0,y1,x1,y2,x2,y3,x3;}`」在功能上等价吗？为什么作者选了前者？

> **参考答案**：功能等价。作者选 `col*8` 这种「展平到一维宽行」的写法，是因为它可以直接作为一个普通 `Mat` 被 `FileStorage` 整体序列化导出（第 258–259 行），也方便后续逐行灌入 FPGA 的 BRAM 当查表；用 struct 反而不利于一键导出和硬件顺序读取。

---

### 4.3 weight 权重表：双线性插值的四个权重

#### 4.3.1 概念说明

知道了去哪 4 个角取像素，还要决定「各取多大比例」。双线性插值的核心思想是：**离哪个角越近，那个角的权重越大**。数学上，权重等于「当前点 与 该角相对的边」围成的小矩形的面积。

设浮点坐标 \((x,y)\)，令 \(x_1=\lfloor x\rfloor,\ x_2=\lceil x\rceil,\ y_1=\lfloor y\rfloor,\ y_2=\lceil y\rceil\)，则 4 个角的权重为：

\[
\begin{aligned}
w_{(x_1,y_1)} &= (x_2-x)(y_2-y) \quad &\text{（左上）}\\
w_{(x_2,y_1)} &= (x-x_1)(y_2-y) \quad &\text{（右上）}\\
w_{(x_1,y_2)} &= (x_2-x)(y-y_1) \quad &\text{（左下）}\\
w_{(x_2,y_2)} &= (x-x_1)(y-y_1) \quad &\text{（右下）}
\end{aligned}
\]

直觉记忆法：某角的权重 = 「它对面的那条边」在 x 和 y 两个方向上的距离之积。例如左上角 \((x_1,y_1)\) 的对面边是 \(x=x_2\) 和 \(y=y_2\)，所以权重是 \((x_2-x)(y_2-y)\)。

把这 4 个权重相加：

\[
(x_2-x)(y_2-y) + (x-x_1)(y_2-y) + (x_2-x)(y-y_1) + (x-x_1)(y-y_1)
\]

提取公因式：

\[
= \bigl[(x_2-x)+(x-x_1)\bigr]\bigl[(y_2-y)+(y-y_1)\bigr] = (x_2-x_1)(y_2-y_1)
\]

当 \((x,y)\) 落在非整数格点之间时 \(x_2-x_1=1,\ y_2-y_1=1\)，**四权重之和恰为 1**，这正是「加权平均、保持亮度」的关键。若某个权重表不满足求和为 1，图像就会整体变亮或变暗——这是检验权重表正确性的第一把尺子。

#### 4.3.2 核心流程

weight 表是一个 `1100` 行、`1086*4 = 4344` 列的 `float` 矩阵（`CV_32FC1`），**每个目标像素占连续 4 个 float**，依次对应 addr 表里的 4 个角：左上、左下、右上、右下。

把取整差值换成几何语言：

| 表达式（代码） | 几何含义 |
|----------------|----------|
| `cvCeil(a)-a` 即 \(x_2-x\) | 点到右边的水平距离 |
| `a-cvFloor(a)` 即 \(x-x_1\) | 点到左边的水平距离 |
| `cvCeil(b)-b` 即 \(y_2-y\) | 点到下边的垂直距离 |
| `b-cvFloor(b)` 即 \(y-y_1\) | 点到上边的垂直距离 |

权重表就是把上述 4 个距离量两两相乘，存成 4 个 float。

#### 4.3.3 源码精读

权重表生成的双重循环：[圆柱面投影.cpp:L195-L206](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L195-L206)。

```cpp
for (int row = 0; row < a.rows; ++row)
    for (int col = 0; col < b.cols; ++col)
    {
        weight.at<float>(row, col*4)   = (cvCeil(b)-b) * (cvCeil(a)-a);   // (y2-y)(x2-x)  左上
        weight.at<float>(row, col*4+1) = (b-cvFloor(b)) * (cvCeil(a)-a);  // (y-y1)(x2-x)  左下
        weight.at<float>(row, col*4+2) = (b-cvFloor(b)) * (a-cvFloor(a)); // (y-y1)(x-x1)  右下?
        weight.at<float>(row, col*4+3) = (cvCeil(b)-b) * (a-cvFloor(a));  // (y2-y)(x-x1)  右上?
    }
```

把 4.3.1 的标准公式和 4.2 的地址顺序对齐，**前两项没问题**：

- `weight[col*4]` = \((y_2-y)(x_2-x)\)，配 `addr` 左上 \((x_1,y_1)\)。标准公式给左上的正是 \((x_2-x)(y_2-y)\)。✅ 完全一致。
- `weight[col*4+1]` = \((y-y_1)(x_2-x)\)，配 `addr` 左下 \((x_1,y_2)\)。标准公式给左下的正是 \((x_2-x)(y-y_1)\)。✅ 完全一致。

> ⚠️ **批判性阅读 · 后两项疑似错位**
>
> 第 3、4 项与 addr 顺序对不上。按 addr 表，`col*4+2` 对应**右上 \((x_2,y_1)\)**，标准权重应为 \((x-x_1)(y_2-y)\)；但代码写的是 \((y-y_1)(x-x_1)\)——把垂直距离写成了 \((y-y_1)\) 而非 \((y_2-y)\)。同理 `col*4+3` 对应**右下 \((x_2,y_2)\)**，标准权重应为 \((x-x_1)(y-y_1)\)；但代码写的是 \((y_2-y)(x-x_1)\)。
>
> 也就是说，**右列两个像素（右上、右下）的 y 方向权重被互换了**。你可以用 4.3.1 的「求和为 1」检验：互换两个 y 因子后总和仍为 1（因为只是把 \((y-y_1)\) 与 \((y_2-y)\) 在右列对调，加和不变），所以图像**不会整体变亮变暗**，只在右列像素上产生轻微的「垂直方向错位」瑕疵——这正是 README 里「图像融合部分的小数权重没有怎么做好」的一个具体落点。这个判断可以由你在 4.3.4 的实践中亲手验证。
>
> 提示：本讲给出的是基于标准双线性公式的推导结论，定位为「供你验证的观察」，而非对作者意图的定论。读者若怀疑，可用一个具体数值点（如 \(x=3.7, y=5.2\)）把 4 个权重手算一遍并对照。

需要强调：发现这类问题正是阅读真实工程代码的价值所在。本项目多处带有「半成品」特征（[u4-l1](u4-l1-dynamic-seam.md) 将集中讨论综合性问题），学会**带着标准答案去对照源码**，是本册一直训练的能力。

#### 4.3.4 代码实践

**实践目标**：用一个具体数值点，亲手算 4 个权重，验证「求和为 1」并检验 4.3.3 提出的错位观察。

**操作步骤**：

1. 取 \(x=3.7,\ y=5.2\)，则 \(x_1=3,\ x_2=4,\ y_1=5,\ y_2=6\)。
2. 按本讲 4.3.1 的**标准公式**手算 4 个权重：

   \[
   \begin{aligned}
   w_\text{左上} &= (4-3.7)(6-5.2) = 0.3 \times 0.8 = 0.24\\
   w_\text{右上} &= (3.7-3)(6-5.2) = 0.7 \times 0.8 = 0.56\\
   w_\text{左下} &= (4-3.7)(5.2-5) = 0.3 \times 0.2 = 0.06\\
   w_\text{右下} &= (3.7-3)(5.2-5) = 0.7 \times 0.2 = 0.14
   \end{aligned}
   \]

   求和 \(=0.24+0.56+0.06+0.14=1.00\)。✅
3. 再按**源码第 198–205 行**的实际表达式算（把 `a=3.7, b=5.2` 代入）：

   \[
   \begin{aligned}
   \text{code}_0 &= (6-5.2)(4-3.7) = 0.24\\
   \text{code}_1 &= (5.2-5)(4-3.7) = 0.06\\
   \text{code}_2 &= (5.2-5)(3.7-3) = 0.14\\
   \text{code}_3 &= (6-5.2)(3.7-3) = 0.56
   \end{aligned}
   \]

4. 把两组结果按「左上 / 左下 / 右上 / 右下」的 addr 顺序对齐比较。

**预期结果**：

| 角 | addr 顺序 | 标准权重 | 源码给出的权重（同位） |
|----|-----------|---------|------------------------|
| 左上 \((x_1,y_1)\) | `col*4` | 0.24 | 0.24 ✅ |
| 左下 \((x_1,y_2)\) | `col*4+1` | 0.06 | 0.06 ✅ |
| 右上 \((x_2,y_1)\) | `col*4+2` | **0.56** | **0.14** ❌ |
| 右下 \((x_2,y_2)\) | `col*4+3` | **0.14** | **0.56** ❌ |

你会发现：左列两角完全正确；右列两角的权重数值「对调」了——0.56 和 0.14 互换。这与 4.3.3 的推导一致。两组求和都等于 1，所以不会造成亮度异常，只在右列产生轻微瑕疵。（如要复现，需在本地配置 OpenCV 环境编译 `圆柱面投影.cpp` 并准备示例图；纯手算已能完成本实践，**待本地验证**的是图像视觉效果。）

#### 4.3.5 小练习与答案

**练习 1**：当源坐标 \(x\) 恰好是整数（如 \(x=4.0\)）时，4 个权重会怎样？

> **参考答案**：此时 \(x_1=x_2=4\)，于是 \((x_2-x)=0\) 且 \((x-x_1)=0\)，左上/左下（含 \(x_2-x\)）权重为 0，右上/右下（含 \(x-x_1\)）权重也为 0——但具体哪两个为 0，取决于哪两个角共用这一列。几何上，点落在格点边线上，只在 y 方向需要插值，4 角退化为 2 角。这也说明「求和为 1」在退化情形下仍由非零项保证。

**练习 2**：如果让你修复 4.3.3 指出的错位，最小改动是什么？

> **参考答案**：把第 202–203 行的 `col*4+2` 项的 y 因子由 \((b-\text{cvFloor}(b))\) 改为 \((\text{cvCeil}(b)-b)\)，同时把第 204–205 行的 `col*4+3` 项的 y 因子由 \((\text{cvCeil}(b)-b)\) 改为 \((b-\text{cvFloor}(b))`——即交换右列两项的 y 因子，使其与标准公式一致。

---

### 4.4 .coe 导出与软件→硬件交接

#### 4.4.1 概念说明

软件算好的 addr/weight 表，最终要变成 FPGA 里的查表系数。Xilinx 工具链习惯用 `.coe`（Coefficient）文件给 Block RAM / ROM IP 初始化数据。理想中的 `.coe` 是这样的纯文本：

```
memory_initialization_radix=10;
memory_initialization_vector=1, 2, 3, ...;
```

但本项目的导出方式有点「名不副实」——它用的是 OpenCV 的 `FileStorage`，把 `addr` 这个 `Mat` 以键值形式序列化到一个叫 `addr.coe` 的文件里。这会产生一个看似 `.coe`、实则是 OpenCV 序列化格式（YAML/XML 风格）的文件。要真正喂给 Xilinx IP，还需要一段额外的格式转换。理解这个差距，是「软件参考实现」走向「硬件实现」时常见的工程细节。

另外要注意：本项目只导出了 **addr 地址表**（`addr.coe`），**没有导出 weight 权重表**。原因在下文：硬件端选择把权重「实时算出来」而不是查表。

#### 4.4.2 核心流程

软件→硬件的「交接」其实是分工：

- **地址（去哪取）**：可离线预算、数据量大、适合查表 → C++ 导出 `addr.coe`，硬件查表读取。
- **权重（各占多少）**：可由定点源坐标的小数部分实时算出、数据量也大 → 硬件不查表，而是在线计算。

这条分工线对应到 `圆柱面投影.v`，就是那组名字带「weight」、实则存「整数坐标」的信号。

#### 4.4.3 源码精读

先看 C++ 端的导出：[圆柱面投影.cpp:L258-L259](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L258-L259)。

```cpp
FileStorage fs("addr.coe", FileStorage::WRITE);
fs << "addr" << addr;
```

两行代码把整个 `addr` 矩阵以 `"addr"` 为键写进 `addr.coe`。`FileStorage` 默认按扩展名决定格式，`.coe` 不是它认识的扩展名，因此会落到其默认序列化格式（OpenCV 版本不同可能是 YAML 或 XML 风格），产物**并不是** Xilinx 原生的 `.coe` 文本。所以「导出 coe」在本项目里更多是「把表存下来留作离线分析 / 再加工」，而非「直接给 Vivado 用」。

再看硬件端如何「实时算地址」，对照 [圆柱面投影.v:L93-L100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.v#L93-L100)：

```verilog
weight_y00 = y[55:30];        // floor(y)  左上：行下取整
weight_x00 = x[55:30];        // floor(x)           列下取整
weight_y01 = y[55:30] + 1;    // ceil(y)   左下：行上取整
weight_x01 = x[55:30];        // floor(x)           列下取整
weight_y10 = y[55:30];        // floor(y)  右上：行下取整
weight_x10 = x[55:30] + 1;    // ceil(x)            列上取整
weight_y11 = y[55:30] + 1;    // ceil(y)   右下：行上取整
weight_x11 = x[55:30] + 1;    // ceil(x)            列上取整
```

这里 `x`、`y` 是硬件用 \(K\cdot R^{-1}\) 矩阵（`k_inv0..k_inv8`）乘出来的定点源坐标（见 [圆柱面投影.v:L90-L92](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.v#L90-L92)）。`y[55:30]` 是「截取高位整数部分」，即定点意义的 `floor`；`+1` 就是 `ceil`。

**命名陷阱**：这些信号虽以 `weight_` 开头，存的实际是 4 个邻居的**整数坐标**，而非双线性权重。下标按 `(y, x)` 顺序，第一位 0/1 表示行 floor/ceil，第二位 0/1 表示列 floor/ceil。于是它们与 C++ addr 表的 4 个角**一一对应**：

| C++ addr 表（`col*8` 偏移） | 角 | 硬件信号（`圆柱面投影.v`） |
|-----------------------------|----|---------------------------|
| `addr[0],addr[1]` = (y_floor, x_floor) | 左上 | `weight_y00, weight_x00` |
| `addr[2],addr[3]` = (y_ceil , x_floor) | 左下 | `weight_y01, weight_x01` |
| `addr[4],addr[5]` = (y_floor, x_ceil ) | 右上 | `weight_y10, weight_x10` |
| `addr[6],addr[7]` = (y_ceil , x_ceil ) | 右下 | `weight_y11, weight_x11` |

这张表是本讲最重要的「软件→硬件」对照成果：它把 C++ 里手算的 4 个地址，精确映射到 Verilog 里同样语义的 4 组信号。至于真正的小数权重（4.3 里的那 4 个 float），硬件需要从 `x`、`y` 的小数部分（被截掉的低 30 位）继续算乘法——`圆柱面投影.v` 当前只输出到坐标为止，**最终的加权求和并未在该文件中实现**（这一点在 [u2-l4](u2-l4-cylindrical-projection-hardware.md)、[u5-l1](u5-l1-fixed-point-arithmetic.md) 会继续展开）。

#### 4.4.4 代码实践

**实践目标**：把软件地址表与硬件信号手工对齐，建立「同一段语义、两种实现」的直觉。

**操作步骤**：

1. 打开 [圆柱面投影.v:L48-L55](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.v#L48-L55)（信号声明）与 [圆柱面投影.cpp:L184-L191](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L184-L191)（addr 赋值）。
2. 在笔记本上重画上面那张 4 行对照表，把每一行的「角」标成「左上/左下/右上/右下」。
3. 回答：硬件为什么用 `y[55:30]` 表示 floor，用 `+1` 表示 ceil？提示：定点数高位是整数部分、低位是小数部分；截掉小数部分即下取整。

**预期结果**：你能解释「C++ 的 `cvFloor/cvCeil`」与「Verilog 的 `截高位 / 截高位+1`」是同一件事的两种表达；并理解 4 个 `weight_xN/weight_yN` 信号虽然名字叫 weight，实为 4 个邻居地址。

#### 4.4.5 小练习与答案

**练习 1**：`addr.coe` 能被 Vivado 直接当作 Block RAM 的初始化文件吗？

> **参考答案**：不能直接用。它是 OpenCV `FileStorage` 的序列化产物（YAML/XML 风格的键值文本），而 Vivado 的 `.coe` 要求 `memory_initialization_radix=...; memory_initialization_vector=...;` 这种特定纯文本格式，需要再做一次格式转换。

**练习 2**：为什么硬件端不再为 weight 表做一块查表 ROM？

> **参考答案**：因为权重可由定点源坐标的小数部分（被截掉的低 30 位）通过「相乘」实时算出，逻辑开销比维护一张和图像等大的浮点 ROM 小得多；而 4 个邻居地址只是整数截断+1，更适合在 C++ 端预算后查表。这种「地址查表、权重现算」的分工是资源与计算量的折中。

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这个「**从浮点坐标到 FPGA 查表记录**」的端到端小任务。

**任务**：选定一个目标像素，沿着 `warp` 的数据通路走一遍，产出它对应的「一条 addr 记录 + 一条 weight 记录 + 硬件信号值」，并自查正确性。

**步骤**：

1. 假设某个目标像素反向映射后得到浮点源坐标 \((x,y) = (3.7,\ 5.2)\)（即 `a=3.7, b=5.2`）。
2. **addr 记录**：按 4.2 的规则写出 8 个 int：
   `(5, 3, 6, 3, 5, 4, 6, 4)`，对应 4 个角 `(5,3)/(6,3)/(5,4)/(6,4)`。
3. **硬件信号**：按 4.4 的对照表写出 8 个 Verilog 信号值：
   `weight_y00=5, weight_x00=3, weight_y01=6, weight_x01=3, weight_y10=5, weight_x10=4, weight_y11=6, weight_x11=4`。
4. **weight 记录**：按 4.3.1 的**标准公式**写出正确的 4 个 float（0.24, 0.06, 0.56, 0.14，对应左上/左下/右上/右下）；再写出源码当前实际产出的 4 个 float（0.24, 0.06, 0.14, 0.56），标记出右列两项的错位。
5. **自查**：验证你的 addr 记录里 `(行,列)` 顺序与 OpenCV `s.at<Vec3b>(row,col)` 一致；验证标准权重之和为 1。
6. **延伸思考**：如果把这张 addr 表整行存入 FPGA 的 Block RAM，目标像素序号 `col` 与读地址的关系是什么？（答：读基地址 = `col * 8`，连续读 8 个字。）

**预期结果**：你会得到一张完整对照单，覆盖「C++ 浮点 → addr/weight 表 → Verilog 信号」全链路，并亲手确认了源码在权重表上的一处错位。这张对照单也是你后续阅读 [u2-l4 圆柱面投影的硬件实现](u2-l4-cylindrical-projection-hardware.md) 和 [u5-l1 定点数运算与位宽设计](u5-l1-fixed-point-arithmetic.md) 时的现成索引。

## 6. 本讲小结

- 双线性插值用浮点源坐标周围的 **4 个整数邻居**按距离加权求值；权重满足「四个之和为 1」，是检验正确性的第一把尺子。
- `warp` 函数手写了 addr/weight/res 三张表，其中 `addr` 每像素占 **8 个 int**（4 个 `(行,列)` 对），`weight` 每像素占 **4 个 float**，采用「每像素一条定长记录」的紧凑排列，便于整体导出和硬件顺序读取。
- addr 表的 4 个角依次是左上、左下、右上、右下，对应 `cvFloor/cvCeil` 对 `a`(列) 与 `b`(行) 的组合；前两个权重正确，**后两个权重的 y 因子疑似错位**（右列互换），求和仍为 1 但会带来轻微瑕疵，呼应 README「小数权重没做好」。
- 软件端用 `FileStorage` 把 `addr` 导出为 `addr.coe`，但产物是 OpenCV 序列化格式，**并非 Xilinx 原生 `.coe`**，需二次转换；weight 表不导出。
- 硬件端 `圆柱面投影.v` 的 `weight_x00/weight_y00 … weight_x11/weight_y11` 虽名为 weight，实为 4 个邻居的**整数坐标**，与 C++ addr 表的 4 个角一一对应；真正的定点小数权重需由坐标小数部分继续计算，本讲定位了这一交接边界。

## 7. 下一步学习建议

本讲把「浮点源坐标 → 查表记录」讲透了，但刻意回避了两个更深入的话题，建议按顺序继续：

1. **[u2-l4 圆柱面投影的硬件实现：定点 Verilog](u2-l4-cylindrical-projection-hardware.md)**：看 `圆柱面投影.v` 如何用 CORDIC 算 `sin/cos`、用 `k_inv` 定点矩阵算出源坐标 `x/y`、再截出本讲对应的 4 个邻居地址。本讲的对照表正是 u2-l4 的入门钥匙。
2. **[u5-l1 定点数运算与位宽设计深入](u5-l1-fixed-point-arithmetic.md)**：弄清 `y[55:30]` 为什么是 floor、被截掉的 30 位小数如何参与权重乘法、定点乘法的位宽为何要扩展再截断。那里会把本讲刻意留下的「硬件如何算小数权重」补完。
3. **课外补充**：若你对双线性的「右列错位」想自行验证，可在本地配置 OpenCV，把 `warp` 里的 `res` 与 OpenCV `remap` 结果做差图（`absdiff`），观察瑕疵出现在画面的哪些区域。
