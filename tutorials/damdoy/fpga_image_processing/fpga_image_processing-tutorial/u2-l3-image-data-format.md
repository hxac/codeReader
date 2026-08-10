# u2-l3 图像数据格式：从 GIMP .h 到灰度像素

## 1. 本讲目标

前面两讲（u2-l1、u2-l2）我们搞清楚了「命令」怎么变成字节流送进 `image_processing` 模块。但还有一类数据没有讲——**图像本身**。主机每次运算前都要先 `send_image(...)`，把一张图喂进硬件。那么，这张图在磁盘上长什么样？主机又是怎么把它变成一串能送进硬件的字节的？

本讲专门回答这个问题。我们来看项目里最容易被忽略、却处处都在的一个文件格式：**GIMP 导出的 C 头图像**（`software/images/*.h`）。

学完本讲你应该能够：

1. 说清楚 `image_width` / `image_height` 这两个变量从哪里来、被谁用。
2. 解释 GIMP 为什么把 RGB 像素编码成一串「可打印字符」，以及 `(data[i] - 33)` 这个减法为什么能还原出 6 比特信息。
3. 手算 `HEADER_PIXEL` 宏：给定 4 个字符，推出对应的 3 个 RGB 字节，并理解「4 个字符 = 24 比特 = 3 字节」的位拼接原理。
4. 说明主机为什么只取 `pixel[0]`（R 通道）就把一幅 RGB 图降成了单通道灰度图，以及这种做法的代价。

> 本讲只讲「图像数据格式」，不讲硬件内部如何把字节存进双缓冲——那是 u3 单元的内容。

## 2. 前置知识

- **像素（pixel）**：图像里最小的一个色点。RGB 图里每个像素由 R、G、B 三个分量组成，每个分量通常占 1 字节（0~255）。所以一个 RGB 像素占 3 字节。
- **灰度图（grayscale）**：每个点只有一个亮度值（0 黑 ~ 255 白），占 1 字节。本项目在 FPGA 上处理的就是**单通道灰度图**（见 u1-l1），所以喂进硬件前必须把 RGB 压成单字节。
- **ASCII 与可打印字符**：ASCII 表里 32 是空格、33 是 `!`、48 是 `0`、65 是 `A`、126 是 `~`。所谓「可打印字符」大致就是能在源码字符串里直接写出来、不引发转义麻烦的字符。GIMP 编码刻意把数据映射到这一段，目的是让整张图能塞进一个合法的 C 字符串字面量。
- **位（bit）与字节（byte）**：1 字节 = 8 比特。本讲的关键算式是 \(6 \times 4 = 24 = 8 \times 3\)，即「4 个 6 比特组 = 3 个 8 比特字节」。
- **上一讲建立的认知**：主机通过纯虚基类 `Image_processing` 的 `send_image(uint8_t *)` 把**一维字节数组**送给后端。本讲就看这个字节数组是怎么从 `.h` 文件里「变」出来的。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [software/images/image_fruits_8.h](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h) | 一张 8×8 的示例图像。本讲用它做完整的手算演示，因为它最小、最直观。它声明了 `image_width` / `image_height`、定义了 `HEADER_PIXEL` 宏、并把整张图存成 `header_data` 字符串。 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机程序。它在顶部 `#include` 某个 `images/*.h`，然后在 `main()` 里用一个循环反复调用 `HEADER_PIXEL` 解码图像，**只保留 `pixel[0]`** 存入 `image_input[]`，再交给 `send_image`。 |
| [software/images/image_sequential.h](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_sequential.h) | （对照用）一张调试图，它的 `HEADER_PIXEL` 被**重新定义**成「像素值 = 序号」，用来在硬件上验证地址/读写是否正确。本讲用它说明这个宏并非一成不变。 |

> 关于文件名的一点事实交代：本讲用 `image_fruits_8.h` 当教具，但 `main.cpp` 当前实际 `#include` 的是 `images/image_fruits_64.h`（一张 64×64 的图），见 [software/main.cpp:7](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L7)。所有 `images/*.h` 的格式**完全一样**，只是尺寸和 `header_data` 内容不同，所以用最小的 8×8 来讲解最清楚，结论对 64×64 同样成立。

---

## 4. 核心概念与源码讲解

### 4.1 image_width / image_height：图像尺寸从哪来

#### 4.1.1 概念说明

主机和硬件在很多地方都需要知道「这张图多大」：主机要分配 `image_width*image_height` 个字节的数组、要循环那么多次解码；硬件要知道要读写多少个像素、卷积要扫描多少行多少列。这个尺寸信息**不是写死在 `main.cpp` 里的**，而是由被 `#include` 的那个 `.h` 文件提供的。

每个 `.h` 文件都声明了两个全局变量：

```c
static unsigned int image_width = 8;
static unsigned int image_height = 8;
```

因为是 `#include` 进 `main.cpp` 的，这两个变量在 `main.cpp` 里直接可见，连「图像尺寸」带「图像数据」一起由头文件带进来。切换图像只要改一行 `#include`（[software/main.cpp:5-10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L5-L10)），尺寸就自动跟着变。

#### 4.1.2 核心流程

```text
.h 文件提供:  image_width, image_height, header_data, HEADER_PIXEL
        │
        ▼  (#include)
main.cpp 直接引用:
   - 分配数组:  new uint8_t[image_width*image_height]
   - 解码循环:  for i in 0 .. image_width*image_height
   - 告诉硬件:  img_proc->send_params(image_width, image_height)
```

注意第三个用途：尺寸还会通过 `send_params` 送给硬件（见 [software/main.cpp:39](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L39) 的 `test_add_threshold`）。这就是 u2-l1 讲过的 `COMMAND_PARAM` 命令，硬件拿到宽高后才知道一次运算要处理多大一片存储区。

#### 4.1.3 源码精读

在 [software/images/image_fruits_8.h:3-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L3-L4)，宽高被声明为 `static unsigned int`：

```c
static unsigned int image_width = 8;
static unsigned int image_height = 8;
```

- `static`：文件内链接，避免多个 `.h` 同时被包含时重名冲突（不过本项目每次只 include 一个图像头，所以这里 `static` 主要是 GIMP 导出模板的默认行为）。
- `unsigned int`：宽高都是非负整数，8×8 这张图就是 8 和 8。

主机侧分配三个数组时直接用它们的乘积（[software/main.cpp:232-234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L232-L234)）：

```c
uint8_t *image_input  = new uint8_t[image_width*image_height];
uint8_t *image_input2 = new uint8_t[image_width*image_height];
uint8_t *image_output = new uint8_t[image_width*image_height];
```

可以看到「图像尺寸」这一信息贯穿了**内存分配 → 解码循环 → 通知硬件**三个环节，源头都是这个 `.h` 文件。

#### 4.1.4 代码实践

1. **实践目标**：确认「换图 = 换 include = 换尺寸」这条链路。
2. **操作步骤**：
   - 打开 [software/main.cpp:5-10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L5-L10)，看清当前生效的是第 7 行的 `#include "images/image_fruits_64.h"`，其余几行被注释。
   - 想象把第 7 行注释掉、改用第 9 行的 `image_fruits_8.h`。
3. **需要观察的现象**：`image_width`/`image_height` 的值会从 64 变成 8，于是 `new uint8_t[...]` 分配的字节数从 \(64\times64=4096\) 变成 \(8\times8=64\)。
4. **预期结果**：程序行为不变（只是处理的图变小了），因为后续所有循环都写成 `image_width*image_height`，自动适配。这正是用变量、而不是魔法数字 `64` 的好处。

#### 4.1.5 小练习与答案

- **练习**：如果把 `.h` 里 `image_height` 改成 4，但 `header_data` 字符串长度没变，会发生什么？
  - **答案**：`main()` 的解码循环只跑 `image_width*4` 次，只会消费 `header_data` 的前一半字符，`image_input` 后半部分是未初始化数据。尺寸变量和实际数据长度必须一致，这是 GIMP 导出时由它保证的契约，手改就会破坏。

---

### 4.2 header_data 字符串编码：把像素藏进可打印字符

#### 4.2.1 概念说明

一张 RGB 图有 \(W\times H\) 个像素，每像素 3 字节，共 \(3\times W\times H\) 字节。GIMP 的「C source」导出要把这堆字节**塞进一个 C 字符串字面量**里。直接塞会有麻烦：字节数据里可能出现 `"`、`\`、或不可打印的控制字符（0~31），这些会让字符串字面量语法出错或无法显示。

GIMP 的办法是：**每 6 比特一组，映射成一个可打印字符**。具体地，把 6 比特值 \(v\)（范围 \(0\sim63\)）加上 33，得到 ASCII 码：

\[
\text{ASCII}(c) = v + 33
\]

- \(v=0 \Rightarrow\) ASCII 33 = `'!'`
- \(v=63 \Rightarrow\) ASCII 96 = `'` ` ` `'`（反引号）

这样所有数据都落在 ASCII 33~96 这段「安全可打印」区间里（少数 `"`、`\` 仍需 C 转义，但内容本身不会出现 0~31 这种控制字符）。解码时只要反过来：

\[
v = \text{ASCII}(c) - 33
\]

这就是宏里到处出现的 `(data[i] - 33)` 的来历。

为什么是 6 比特，而不是 4 比特或 8 比特？因为 6 和 8 的最小公倍数是 24，正好是 3 个字节。于是自然得到「**4 个 6 比特字符 ↔ 3 个 8 比特字节**」的换算关系（下一节详述）。这其实是一种类似 base64 的思路，只是字母表更简单（直接用 `!` 起算的连续 ASCII）。

#### 4.2.2 核心流程

```text
编码（GIMP 导出时做，我们只读结果）:
   3 个 RGB 字节 (24 比特)
        │  按 6 比特切分
        ▼
   4 个 6 比特组 v0..v3
        │  每个 +33 转成可打印字符
        ▼
   字符串里的 4 个字符  "U.$1"

解码（HEADER_PIXEL 宏做，见 4.3）:
   字符串里的 4 个字符
        │  每个 -33 还原 6 比特
        ▼
   重新拼回 3 个 RGB 字节
```

#### 4.2.3 源码精读

图像数据存放在 [software/images/image_fruits_8.h:14-19](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L14-L19)：

```c
static const char *header_data =
    "U.$1M\\/TS=H*Q]0$LK[OL;WN>(2UG:G:J;7FO<GZ[?HJM<'RKKKKIK+CFJ;7HZ_@"
    "Q]0$Y_0DW.D9R-4%Q-$!KKKKDI[/?XN\\T]`0V>86V^@8U>(2O,CYN,3UA9'\"AI+#"
    "KKKKK+CIFZ?8GZO<GZO<DI[/@HZ_A)#!L[_PE:'2A)#!B97&A)#!AI+#CYO,;7FJ"
    "S-D)<7VN?(BYB97&>X>X?8FZ8&R=<7VN:76F;'BI6V>83%B)86V>97&B5F*3;7FJ"
    "";
```

几个要点：

- 这是 4 段相邻的 C 字符串字面量，编译器会**自动拼接**成一个大字符串，末尾的 `""` 是 GIMP 模板用来收尾的空串（无实际内容）。
- 字符总数：8×8 像素 × 每像素 4 字符 = 256 个字符（拼接后）。
- 注意 `\\/` 和 `\"`：这是 C 转义。`\\` 表示一个真正的反斜杠字符（ASCII 92），`\"` 表示一个真正的双引号字符（ASCII 34）。所以**解码时遇到的是一个反斜杠字符**，它的 ASCII 是 92，减 33 得 59，是一个合法的 6 比特值。转义只影响「源码怎么写」，不影响「数据是什么」。

每个字符都落在 33~96 区间：例如 `'U'`=85、`'.'`=46、`'$'`=36、`'1'`=49、`'K'`=75、`'`'`=96，全都满足 \(33 \le \text{ASCII} \le 96\)，减 33 后得到 \(0\sim63\) 的 6 比特值。

#### 4.2.4 代码实践

1. **实践目标**：确认 `header_data` 的字符数与图像尺寸一致。
2. **操作步骤**：数一下 [software/images/image_fruits_8.h:15-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L15-L18) 四行字符串拼接后的字符数（注意把 `\\` 算 1 个字符、`\"` 算 1 个字符）。
3. **需要观察的现象**：拼接后应正好 256 个字符。
4. **预期结果**：\(256 = 4 \times 64 = 4 \times (8\times 8)\)，与「每像素 4 字符、共 64 像素」吻合。若手数不便，可用公式反推：字符数 \(= 4 \times W \times H\)。
5. 若无法在本地精确数出（转义字符容易数错），明确标注「待本地验证」，但公式 \(4 \times W \times H\) 是确定的。

#### 4.2.5 小练习与答案

- **练习**：为什么编码偏移量是 33，而不是 0 或 48？
  - **答案**：偏移 0 会把 \(v=0\) 映射成 ASCII 0（空字符 `\0`），它会让 C 字符串提前结束；偏移到 33（`!`）则把整个 \(0\sim63\) 区间映射到 33~96，全部是可打印、且不是 `\0` 的字符，保证字符串完整。
- **练习**：最大字符 ASCII 96 对应的 6 比特值是多少？
  - **答案**：\(96-33=63\)，正好是 6 比特能表示的最大值（\(2^6-1\)）。

---

### 4.3 HEADER_PIXEL 解包宏：4 个字符 → 3 个字节

> 这是本讲的核心，也是规格指定的必做实践所在。

#### 4.3.1 概念说明

`HEADER_PIXEL` 是一个**宏**，它每次「吃掉」字符串里的 4 个字符，吐出 3 个字节（一个 RGB 像素），并把指针前进 4。反复调用就能逐个解出全部像素。GIMP 在头文件注释里也说明了这一点：*Call this macro repeatedly. After each use, the pixel data can be extracted*（[software/images/image_fruits_8.h:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L6)）。

数学上，它完成的是「6 比特组 → 8 比特字节」的重打包。设 4 个字符减 33 后得到的 6 比特值为 \(a, b, c, d\)，共 24 比特：

\[
\underbrace{a_5\ldots a_0}_{a}\;
\underbrace{b_5\ldots b_0}_{b}\;
\underbrace{c_5\ldots c_0}_{c}\;
\underbrace{d_5\ldots d_0}_{d}
\quad\text{(共 24 比特)}
\]

重新按 8 比特切分，得到 3 个字节：

\[
\begin{aligned}
\text{byte0} &= a_5 a_4 a_3 a_2 a_1 a_0\; b_5 b_4 \\
\text{byte1} &= b_3 b_2 b_1 b_0\; c_5 c_4 c_3 c_2 \\
\text{byte2} &= c_1 c_0\; d_5 d_4 d_3 d_2 d_1 d_0
\end{aligned}
\]

也就是说：第 1 个字节由「\(a\) 全部 6 位 + \(b\) 的高 2 位」组成；第 2 个字节由「\(b\) 的低 4 位 + \(c\) 的高 4 位」组成；第 3 个字节由「\(c\) 的低 2 位 + \(d\) 全部 6 位」组成。宏里的位移与掩码，就是在做这件事。

#### 4.3.2 核心流程

```text
输入: data 指向 4 个字符 c0 c1 c2 c3
   a = c0 - 33   (6 比特)
   b = c1 - 33   (6 比特)
   c = c2 - 33   (6 比特)
   d = c3 - 33   (6 比特)

输出:
   pixel[0] = (a << 2) | (b >> 4)        # 取 a 全部 + b 高 2 位
   pixel[1] = ((b & 0xF) << 4) | (c >> 2)# 取 b 低 4 位 + c 高 4 位
   pixel[2] = ((c & 0x3) << 6) | d       # 取 c 低 2 位 + d 全部

副作用: data += 4   # 指针前进，为下一个像素做准备
```

掩码的含义：

- `& 0xF`（即 `& 0b1111`）：取 6 比特值里的**低 4 位**。
- `& 0x3`（即 `& 0b11`）：取 6 比特值里的**低 2 位**。
- `>> 4` / `>> 2`：把高 2 位 / 高 4 位移到最低位，方便拼接。

#### 4.3.3 源码精读

宏定义在 [software/images/image_fruits_8.h:8-13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L8-L13)：

```c
#define HEADER_PIXEL(data,pixel) {\
pixel[0] = (((data[0] - 33) << 2) | ((data[1] - 33) >> 4)); \
pixel[1] = ((((data[1] - 33) & 0xF) << 4) | ((data[2] - 33) >> 2)); \
pixel[2] = ((((data[2] - 33) & 0x3) << 6) | ((data[3] - 33))); \
data += 4; \
}
```

逐行对应：

- 第 9 行 `pixel[0]`（[L9](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L9)）：`(data[0]-33)<<2` 把 \(a\) 放到字节的高 6 位，`(data[1]-33)>>4` 取 \(b\) 的高 2 位补到低 2 位，二者或起来就是 byte0。
- 第 10 行 `pixel[1]`（[L10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L10)）：`(data[1]-33)&0xF` 取 \(b\) 的低 4 位左移到高半字节，`(data[2]-33)>>2` 取 \(c\) 的高 4 位放低半字节，拼成 byte1。
- 第 11 行 `pixel[2]`（[L11](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L11)）：`(data[2]-33)&0x3` 取 \(c\) 的低 2 位左移到最高 2 位，再直接或上 \(d\)（`data[3]-33`，本身 6 位）凑成 byte2。
- 第 12 行 `data += 4`（[L12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L12)）：指针前进 4 个字符，下次调用就从下一个像素的 4 个字符开始。

**手算验证（第 0 个像素）**：取 `header_data` 前 4 个字符 `"U.$1"`。

| 字符 | ASCII | 减 33（6 比特值） | 角色 |
| --- | --- | --- | --- |
| `U` | 85 | 52 | \(a=52\) |
| `.` | 46 | 13 | \(b=13\) |
| `$` | 36 | 3 | \(c=3\) |
| `1` | 49 | 16 | \(d=16\) |

代入宏：

\[
\begin{aligned}
\text{pixel[0]} &= (52 \ll 2) \;|\; (13 \gg 4) = 208 \;|\; 0 = 208 \\
\text{pixel[1]} &= ((13 \,\&\, 15) \ll 4) \;|\; (3 \gg 2) = (13 \ll 4) \;|\; 0 = 208 \\
\text{pixel[2]} &= ((3 \,\&\, 3) \ll 6) \;|\; 16 = (3 \ll 6) \;|\; 16 = 192 \;|\; 16 = 208
\end{aligned}
\]

所以第 0 个像素 = **(R=208, G=208, B=208)**，一个浅灰色点。手算结果与宏逻辑完全自洽。

> **旁注：宏并非一成不变。** 调试图 [software/images/image_sequential.h:8-13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_sequential.h#L8-L13) 把 `HEADER_PIXEL` 重定义成 `pixel[0]=pixel[1]=pixel[2]=((data-header_data)/4)`——即「第几个像素就取值几」，完全忽略编码。这说明 `HEADER_PIXEL` 只是 GIMP 模板给的默认解码器，用户可以替换它来制造特殊测试图。

#### 4.3.4 代码实践（本讲必做）

这是本讲的核心实践，目标是亲手验证「4 个字符 → 3 个字节」。

1. **实践目标**：手算 `header_data` 前 4 个字符 `"U.$1"` 解出的 RGB 值，并解释位拼接原理。
2. **操作步骤**：
   - 查 ASCII 表得到 `'U'=85, '.'=46, '$'=36, '1'=49`。
   - 各减 33，得到四个 6 比特值 \(52, 13, 3, 16\)。
   - 按 4.3.2 的公式算出 `pixel[0]/pixel[1]/pixel[2]`。
3. **需要观察的现象**：三个分量都等于 208。
4. **预期结果**：`(208, 208, 208)`。这与上文 4.3.3 的手算一致，说明「4 个 6 比特字符 = 24 比特 = 3 个 8 比特字节」的换算在宏里被忠实实现。
5. **延伸思考**：把 4 个 6 比特值写成二进制 \(52=110100,\ 13=001101,\ 3=000011,\ 16=010000\)，首尾相接得 `110100 001101 000011 010000`，再按 8 位重切为 `11010000 11010000 11010000`，即 208、208、208——与逐字节公式结果完全相同，印证 \(6\times4=8\times3\)。

> 本实践为「源码阅读 + 手算」型，无需运行硬件或仿真。若想用程序核验，可写一个小 C 程序 `#include "images/image_fruits_8.h"` 后循环调用 `HEADER_PIXEL` 打印每个像素（编译方式参照 u1-l3 的 `g++` 命令），但需注意 `main.cpp` 已定义 `main`，需另起文件以免冲突——这一步为可选，**待本地验证**。

#### 4.3.5 小练习与答案

- **练习**：为什么 `pixel[0]` 用的是 `(b >> 4)` 而 `pixel[1]` 用的是 `(b & 0xF)`？
  - **答案**：6 比特的 \(b\) 被拆成两半——高 2 位（`b>>4` 取到的是 `b5b4`，因为 6 位右移 4 剩高 2 位）拼进 byte0 的低位；低 4 位（`b & 0xF` 取 `b3b2b1b0`）拼进 byte1 的高位。一个 \(b\) 被分给两个字节，所以两处分别取它的高 2 位和低 4 位，合起来仍是完整的 6 位，不重不漏。
- **练习**：第 1 个像素对应的 4 个字符是 `"M\/T"`（即 `M`、`\`、`/`、`T`），手算它的 R 值。
  - **答案**：ASCII 为 77、92、47、84，减 33 得 \(a=44,b=59,c=14,d=51\)。`pixel[0] = (44<<2)|(59>>4) = 176|3 = 179`。所以第 1 个像素 R=179。

---

### 4.4 RGB → 单通道灰度：为什么只取 pixel[0]

#### 4.4.1 概念说明

`HEADER_PIXEL` 解出来的是完整的 RGB 三字节，但本项目在 FPGA 上只做**单通道灰度**处理（见 u1-l1：片上内存只有 1Mbit，8 位像素刚好够用）。所以主机必须把每个像素从 3 字节压成 1 字节。

最严谨的灰度化是按人眼亮度做加权平均（亮度公式）：

\[
Y = 0.299\,R + 0.587\,G + 0.114\,B
\]

但本项目没用这个公式，而是用了最简单的办法——**直接取 R 通道**（`pixel[0]`）。这是一种「就近取用」的降采样：丢掉 G、B，只留 R 当作灰度值。

为什么可以这么粗暴？因为本项目的测试图（如 `image_fruits_8.h`）解码出来的 RGB 三分量相等（第 0 像素是 (208,208,208)，见 4.3 手算），源内容本身就是灰度的，取哪个通道都一样、没有信息损失。但要注意：**如果换一张真彩色照片，这种做法就会丢失颜色信息**，只是「近似灰度」。这是项目为了简单而做的取舍。

#### 4.4.2 核心流程

```text
对每个像素 i = 0 .. W*H-1:
   HEADER_PIXEL(ptr_image, pixel)   # pixel[0..2] = R,G,B
   image_input[i] = pixel[0]        # 只存 R，丢掉 G、B
   # ptr_image 已在宏里 += 4，自动指向下一个像素

结果: image_input[] 是一维单通道灰度数组 (W*H 字节)
      → 交给 img_proc->send_image(image_input)
```

#### 4.4.3 源码精读

解码循环在 [software/main.cpp:236-241](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L236-L241)：

```c
const char *ptr_image = header_data;
for (size_t i = 0; i < image_height*image_width; i++) {
    uint8_t pixel[3];
    HEADER_PIXEL(ptr_image, pixel);
    image_input[i] = pixel[0];
}
```

- 第 236 行：`ptr_image` 从 `header_data` 头部开始，作为「读指针」。
- 第 237 行：循环 `image_width*image_height` 次——**这正是「把一幅 RGB 图降为单通道灰度图」的关键**。每轮处理一个像素，共 \(W\times H\) 个。
- 第 239 行：调用宏，解出当前像素的 RGB，宏内部把 `ptr_image` 前进 4。
- 第 240 行：**只保存 `pixel[0]`（R 通道）**到 `image_input[i]`，`pixel[1]`（G）、`pixel[2]`（B）被丢弃。

由此，`image_input[]` 从「每元素 3 字节的 RGB」被压成「每元素 1 字节的灰度」，大小恰好 \(W\times H\) 字节。这个数组随后由 `send_image(image_input)` 送进硬件（如 [software/main.cpp:48](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L48)），与本讲 u2-l2 讲过的「参数用小端 16 位」不同，图像字节是**逐字节原样发送**的，每个字节就是一个 0~255 的灰度像素。

> 顺带一提：`image_input2`（第二张图）的解码在同一段被注释掉了（[software/main.cpp:244-249](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L244-L249)），逻辑完全相同，只是用另一个 `header_data2`。只有 `test_images_average` / `test_images_diff` 这类双图测试才需要它。

#### 4.4.4 代码实践

1. **实践目标**：理解「循环 \(W\times H\) 次、只存 `pixel[0]`」如何完成 RGB→灰度的降维。
2. **操作步骤**：
   - 阅读 [software/main.cpp:237-240](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L237-L240)。
   - 假设把第 240 行改成 `image_input[i] = (pixel[0] + pixel[1] + pixel[2]) / 3;`（三通道算术平均）。
3. **需要观察的现象**：对当前测试图（RGB 三分量相等），改与不改结果**完全一样**（因为 \(R=G=B\) 时，平均仍等于该值）。
4. **预期结果**：验证「取 R 通道」在本项目测试图上等价于平均法，没有损失；同时也说明，对真彩色图这两种做法才会产生差异。
5. 若要实际跑：参照 u1-l3 用 Verilator 仿真模式构建运行，并用 `run_gnuplot.sh` 查看 `output.dat`——但本步骤为可选，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习**：如果想让灰度更贴近人眼感受，应把第 240 行改成什么？
  - **答案**：用亮度加权 `image_input[i] = (uint8_t)(0.299*pixel[0] + 0.587*pixel[1] + 0.114*pixel[2]);`。但注意本项目测试图 \(R=G=B\)，结果不变；改进只在真彩色图上才有意义。
- **练习**：`image_input` 数组的总字节数与 `header_data` 的字符数是什么关系？
  - **答案**：`image_input` 有 \(W\times H\) 字节；`header_data` 有 \(4\times W\times H\) 个字符。所以**字符数是灰度字节数的 4 倍**，也即每 4 个字符产出 1 个灰度字节（中间先还原成 3 个 RGB 字节、再丢弃 2 个）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从字符串到灰度数组」的完整追踪。

**任务**：以 `image_fruits_8.h` 的第 0 个像素（`"U.$1"`）和第 1 个像素（`"M\/T"`）为例，完整复现主机解码全过程，并对照 `main.cpp` 的循环。

要求：

1. 列出每个字符的 ASCII、减 33 后的 6 比特值。
2. 用 24 比特二进制串（`a b c d` 首尾相接）和「8 位重切」两种方法，分别推出 3 个 RGB 字节，确认两者一致。
3. 指出 `main.cpp:240` 会把这两个像素的哪一个字节存进 `image_input[0]` 和 `image_input[1]`。
4. 回答：若把这张 8×8 图送进硬件，`send_image` 实际会送多少个字节？为什么不是 \(3\times 8\times 8\)？

**参考答案要点**：

1. 第 0 像素 \(a,b,c,d = 52,13,3,16\)；第 1 像素 \(a,b,c,d = 44,59,14,51\)。
2. 第 0 像素 24 比特串 `110100 001101 000011 010000`，重切为 `11010000 11010000 11010000` = (208,208,208)；与逐字节公式一致。第 1 像素同理得 R=179。
3. `image_input[0]=208`、`image_input[1]=179`（都是 R 通道）。
4. 送 \(8\times 8 = 64\) 个字节。因为主机已把每个 RGB 像素压成 1 个灰度字节（只取 R），所以字节数是 \(W\times H\)，而非 \(3\times W\times H\)。

## 6. 本讲小结

- 图像尺寸 `image_width` / `image_height` 来自被 `#include` 的 `.h` 文件，贯穿数组分配、解码循环和 `send_params` 通知硬件三个环节。
- GIMP 把 RGB 字节按 **6 比特一组**编码成可打印字符，偏移量 33 把 \(0\sim63\) 映射到 ASCII 33~96，解码就是 `(char - 33)`。
- 关键换算 \(6\times 4 = 24 = 8\times 3\) 决定了 **4 个字符正好解出 3 个 RGB 字节**；`HEADER_PIXEL` 宏用移位和掩码（`<<`、`>>`、`& 0xF`、`& 0x3`）完成这次位重打包，并把指针前进 4。
- 主机循环 \(W\times H\) 次，每次只保存 `pixel[0]`（R 通道），把 RGB 图压成单通道灰度数组；对项目自带的灰度测试图无损，对真彩色图则是近似。
- 最终 `image_input[]` 是一维 \(W\times H\) 字节灰度数组，由 `send_image` 逐字节送进硬件，与命令参数的「小端 16 位」编码不同。
- `header_data` 字符数 \(= 4\times W\times H\)，即灰度字节数的 4 倍。

## 7. 下一步学习建议

本讲把「图像在主机侧是什么形态」讲完了。接下来自然有两个方向：

- **向上（接口层）**：`send_image` 内部到底把字节怎么打包、怎么发？这属于 u2-l2 已讲的命令协议——图像字节走的是 `comm_data_in` 逐字节通道，可以回头对照 `simulation/image_processing_simulation.cpp` 与 `ice40/software/image_processing_ice40.cpp` 里 `send_image` 的实现，确认两套后端送的灰度字节完全一致。
- **向下（硬件层）**：这些灰度字节进到 `image_processing.v` 后被存进哪里？怎么用 16 位字两个像素打包存？这正是下一单元 u3 的内容——建议从 **u3-l1（端口与两大接口）** 和 **u3-l2（双缓冲存储模型与 16 位像素打包）** 开始，看主机送来的灰度字节如何落进 input/storage 双缓冲。

建议阅读的源码：[hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) 的 `STATE_SEND_IMG` 状态（接收图像字节、两两打包成 16 位字写入存储）。
