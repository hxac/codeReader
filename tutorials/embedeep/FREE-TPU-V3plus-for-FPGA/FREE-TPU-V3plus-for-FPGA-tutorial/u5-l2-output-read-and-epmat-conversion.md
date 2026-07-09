# 输出读取与 epmat→ncnn::Mat 转换

## 1. 本讲目标

上一讲（u5-l1）我们把一次推理在时间轴上跑完了：写 4 个 `BASEADDR`、写 `ALGOADDR`、写 `STARTUP=0x11`、忙等轮询 `STATUS` 的 bit31。轮询通过的那一刻，TPU 已经把**计算结果**写进了 DDR 里 `out` 那一段内存。但这些结果并不是 CPU 能直接拿来用的浮点数组——它们是 TPU 自有的 **epmat 格式**：定点 int16、按 16 通道分组、以 32 字节为步长排列。

本讲要回答的问题是：**forward 完成之后，软件如何把 TPU 的输出读回来，并翻译成后处理能直接消费的浮点 `ncnn::Mat`？**

学完本讲，你应当能够：

1. 说出 `read_forward_result` 的完整流程：从 `addr_out` 取地址、算大小、`mem_read` 读 DDR、转 `ncnn::Mat`、归还缓冲。
2. 解释 **epmat 的内存布局**：16 通道分组、每个空间位置 32 字节、int16 小端定点，并能用 `epmat_get_size` 算出任意输出张量的字节大小。
3. 读懂 `epmat2nmat` 的双层循环与寻址公式 `curr_ch_group=c/16`、`epmat_offset=(...+c%16)*2`、`+=32`，并写出从 epmat 还原一个 float 值的公式。
4. 理解裸机自实现的 `ncnn::Mat` 如何用 `channel(c)` 视图与 `cstep` 对齐来承接还原后的浮点数据。

## 2. 前置知识

本讲是裸机路线的「最后一公里」，需要你已建立以下认知（来自前置讲义）：

- **两条 AXI 通路**（u4-l3、u1-l3）：控制通路（PS→TPU 寄存器，落在 `0xA0000000`）和数据通路（TPU 经 HP 口访问 DDR）。本讲的 `eepif.mem_read(...)` 走的就是数据通路，把 DDR 里的输出张量搬回 CPU 内存。
- **EEP_INTERFACE 是底层搬运工**（u4-l3）：`mem_read/mem_write` 用 `volatile unsigned char` 逐字节拷贝，不要求地址对齐；`register_*` 走控制通路。本讲里 `EEPTPU_SA` 通过成员 `eepif` 调用它们。
- **硬件输入的打包格式**（u4-l4）：输入侧把图像打成「16 通道 × 2 字节 = 32 字节」为最小访存单元的格式，每个空间位置独占一个 32 字节槽位，多余通道清零。**epmat 输出用的是同一套 16 通道分组 / 32 字节步长布局**——这是贯穿本讲的核心对称性。
- **eepnet_config 与 st_hwaddr_info**（u3-l3）：`eeptpu_init` 在初始化时把每个输出张量的 `{hwaddr, shape[4], exp}` 解析进 `addr_out` 数组，本讲直接消费它。
- **forward 写了 BASEADDR3**（u5-l1）：`pub` 型（bin_type=2）写 4 段基址 par/in/tmp/out，TPU 把结果写进 `out` 段（即 `hwbase3` / `addr_out[i].hwaddr` 指向的区域）。

> 术语速查
> - **epmat**：EEP-TPU 的原生输出张量格式，定点 int16 + 16 通道分组。
> - **exp（定点指数）**：硬件用 `value_int16 / 2^exp` 近似一个浮点数，`exp` 由网络在编译期确定（如 yolov4-tiny 输出 exp=8）。
> - **ncnn::Mat**：ncnn 推理框架的张量类型，裸机工程自带了一个最小实现（`nmat.h`），后处理算子都以它为输入输出。

## 3. 本讲源码地图

本讲集中在两个文件，外加几处必要的引用：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| `sdk/standalone/src/eeptpu/eeptpu_sa.cpp` | 裸机 TPU 驱动核心 | `read_forward_result`、`epmat_get_size`、`epmat2nmat`、`epmat2nmat_simple` |
| `sdk/standalone/src/eeptpu/nmat.h` | 自实现的 `ncnn::Mat` 与对齐内存工具 | `Mat::channel()`、`Mat::create(w,h,c)`、`fastMalloc`、`alignSize` |
| `sdk/standalone/src/eeptpu/eeptpu_sa.h` | `EEPTPU_SA` 类与 `st_hwaddr_info` 结构 | `addr_out`、`st_hwaddr_info` 定义 |
| `sdk/standalone/src/eeptpu/interface/eep_interface.h` | 底层 AXI 读写 + `round_up` 宏 | `mem_read` 接口、`round_up` 宏定义 |
| `sdk/standalone/src/main.cc` | 菜单主流程 | 调用 `read_forward_result` 并把结果喂给后处理 |
| `sdk/standalone/src/config.h` | 编译期地址与开关 | `EEPTPU_MEM_BASE_ADDR` |

一句话定位：**`eeptpu_sa.cpp` 负责「读 DDR + 转格式」，`nmat.h` 提供「转完之后存到哪」。**

## 4. 核心概念与源码讲解

### 4.1 read_forward_result：把 TPU 输出从 DDR 读回内存

#### 4.1.1 概念说明

`forward()` 跑完后，结果已经在硬件里，但**不在 CPU 能直接访问的变量里**，而是在 DDR 的某段物理地址上（由 `addr_out[i].hwaddr` 指明）。`read_forward_result` 就是这座桥：它逐个把每个输出张量从 DDR 搬到一块临时 malloc 的缓冲区 `epmat`，再把这块「硬件格式」的缓冲区翻译成「软件格式」的 `ncnn::Mat`，塞进输出列表 `outputs`。

为什么要分「读」和「转」两步？因为 `eepif.mem_read` 只会按字节搬运，它不知道也不关心格式；而格式翻译（`epmat2nmat`）是纯 CPU 计算，需要一块连续的可读缓冲。两步分离让底层搬运与上层翻译各司其职。

#### 4.1.2 核心流程

`read_forward_result` 的伪代码：

```
对 addr_out 里的每个输出张量 i：
    1. epmat_size = epmat_get_size(C, H, W)      // 按 16 通道对齐算字节数
    2. epmat = malloc(epmat_size); memset(0)     // 临时缓冲
    3. eepif.mem_read(addr_out[i].hwaddr, epmat, epmat_size)  // DDR → CPU
    4. out = epmat2nmat(C, H, W, epmat, exp)     // 硬件格式 → 浮点 Mat
    5. outputs.push_back(out)
    6. free(epmat)                               // 临时缓冲用完即还
```

注意 `addr_out` 里每个元素是一个 `st_hwaddr_info`，它的 `shape[1..3]` 分别是 C、H、W（`shape[0]` 是 N，恒为 1，不参与读取）。

#### 4.1.3 源码精读

`addr_out` 在 `eeptpu_init` 里由 eepnet_config 数组解析而来，每个输出张量记录了硬件地址、NCHW 形状和定点指数：

[eeptpu_sa.cpp:146-157](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L146-L157) — 逐字段读出 `hwaddr`(加 `mem_base` 得绝对地址)、`shape[0..3]`、`exp`，压入 `addr_out`。这就是本讲「按地址与 shape 读取」的地址与 shape 的来源。

主体 `read_forward_result`：

[eeptpu_sa.cpp:364-387](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L364-L387) — 主体循环：`epmat_get_size` 算大小 → `malloc`+`memset` → `eepif.mem_read` 从 `addr_out[i].hwaddr` 读 → `epmat2nmat` 转 `ncnn::Mat` → `push_back` → `free`。

关键三行（精简）：

```cpp
unsigned int epmat_size = epmat_get_size(shape[1], shape[2], shape[3]); // C,H,W
unsigned char* epmat = (unsigned char*) malloc(epmat_size);
ret = eepif.mem_read(addr_out[i].hwaddr, epmat, epmat_size);            // DDR -> CPU
out = epmat2nmat(shape[1], shape[2], shape[3], epmat, addr_out[i].exp);// -> float Mat
```

注意 `shape[1]/shape[2]/shape[3]` 被当作 `channel/height/width` 传给后续函数——这正是 NCHW 约定。`mem_read` 的底层实现见 u4-l3（走数据通路、`volatile` 逐字节拷贝）。

调用方在 `main.cc` 菜单选项 `'2'`（forward）里：

[main.cc:396-402](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L396-L402) — forward 完成后立即调用 `eepsa.read_forward_result(outputs)`，随后按 `NET_TYPE` 把 `outputs` 喂给分类 `get_topk` 或检测 `yolo3_detection_output_forward`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：搞清一次 `read_forward_result` 到底读了几个张量、每个多大。
2. **操作步骤**：
   - 打开 `eeptpu_sa.cpp` 的 `read_forward_result`，确认它遍历的是 `addr_out.size()`。
   - 回到 `eeptpu_init` 里的 `printf("output cnt = %d\n", cnt_out)`（L144）与循环里 `printf("out[%d]: hwaddr ... shape: ...")`（L155）。
   - 对照 [config.h:45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L45) `NET_TYPE` 当前是 `NetType_Object_Detect`。
3. **需要观察的现象**：上电初始化时串口会打印 `output cnt = 2` 以及两个 `out[0]/out[1]` 的地址和 shape。
4. **预期结果**：对 yolov4-tiny，应看到 2 个输出，shape 分别接近 `[1,255,13,13]` 与 `[1,255,26,26]`（数值以你的模型为准）。
5. 若无硬件：标注「待本地验证」，仅做静态阅读。

#### 4.1.5 小练习与答案

**练习 1**：`read_forward_result` 里 `epmat` 缓冲为什么要在 `mem_read` 之后、`push_back` 之后 `free` 掉？能不能不 free？

> **答案**：`epmat` 只是「读 DDR 的临时落点」和「翻译的输入」，翻译结果已经复制进了 `ncnn::Mat` 自己管理的内存（见 4.4），所以临时缓冲使命完成即可释放。不 free 会导致每次推理泄漏 `epmat_size` 字节——单张推理尚可，实时 demo 循环（菜单 `'5'`）会很快耗尽堆。

**练习 2**：为什么传给 `epmat_get_size` 和 `epmat2nmat` 的是 `shape[1]/shape[2]/shape[3]` 而不是 `shape[0]`？

> **答案**：`shape` 是 NCHW，`shape[0]` 是 batch 维（裸机下恒为 1），`shape[1/2/3]` 才是真正的 C/H/W。epmat 的布局只与 C/H/W 有关，batch=1 不参与寻址。

---

### 4.2 epmat 的内存布局与 epmat_get_size 尺寸计算

#### 4.2.1 概念说明

epmat 是 TPU 的**原生输出格式**，它和输入侧的打包格式（u4-l4）是同一套思路：硬件以「**16 通道 × 2 字节 = 32 字节**」为最小访存单元。具体说：

- 每个数值是 **int16 定点**（2 字节，小端序），真实浮点值 = `int16 / 2^exp`。
- 通道按 **16 个一组**打包；不足 16 的也要补齐到 16（补的字节无意义，故 `memset(0)`）。
- 在一个 16 通道组内，**同一个空间位置 (h,w) 的 16 个通道连续存放**（占 32 字节），下一个空间位置紧随其后再占 32 字节。
- 一个组写完所有 H×W 个位置后，才轮到下一个 16 通道组。

这就解释了为什么读第 `c` 个通道要先 `c/16` 定位「组」、再用 `c%16` 定位「组内的第几个通道」——详见 4.3。

#### 4.2.2 核心流程

`epmat_get_size` 给定 `(c,h,w)`，返回这块 epmat 在 DDR 里占多少字节。核心是把通道数 `c` 向上取整到 16 的倍数（`round_up`），再乘以 `h*w` 个位置、每个位置 16 通道 × 2 字节。

对齐辅助宏 `round_up`：

\[ \text{round\_up}(c, 16) = ((c-1)\ \mid\ 15) + 1 = \lceil c/16 \rceil \times 16 \]

尺寸公式（一般情况）：

\[ \text{epmat\_size} = H \times W \times \text{round\_up}(C, 16) \times 2 \]

特殊退化为 1×1（向量）时：`round_up(c,16)*2`。

#### 4.2.3 源码精读

`round_up` 宏定义在接口头里，用位运算做向上取整：

[eep_interface.h:45-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L45-L46) — `__round_mask` 造出 `y-1` 的掩码，`(x-1)|mask` 把低于对齐位的 bit 全置 1，再 `+1` 进位，得到大于等于 `x` 的最小 `y` 的倍数。

`epmat_get_size`：

[eeptpu_sa.cpp:309-313](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L309-L313) — 1×1 退化走 `round_up(c,16)*2`；一般情况 `h*w*round_up(c,16)*2`。`*2` 即每个定点值 2 字节。

```cpp
int epmat_get_size(int c, int h, int w)
{
    if (h == 1 && w == 1) return round_up(c, 16) * 2;
    else return (h * w * round_up(c, 16)  * 2);
}
```

**算个例子**（yolov4-tiny 两输出，C=255）：

| 输出 | C | H | W | round_up(255,16) | epmat_size |
| --- | --- | --- | --- | --- | --- |
| out0 | 255 | 13 | 13 | 256 | 13×13×256×2 = **86,528** 字节 |
| out1 | 255 | 26 | 26 | 256 | 26×26×256×2 = **346,112** 字节 |

`round_up(255,16)=256`，意味着 255 个真实通道被补到 256（多出 1 个通道槽位是无用填充）。这两块 epmat 大小，正是 `read_forward_result` 里 `malloc` 和 `mem_read` 的字节数。

#### 4.2.4 代码实践（计算型）

1. **实践目标**：亲手算出 yolov4-tiny 输出的 epmat 大小，并与 `round_up` 行为对应。
2. **操作步骤**：
   - 对 `c=255` 手算 `(255-1)|15)+1 = (254|15)+1 = 255+1 = 256`。
   - 对 `c=1000`（分类网络）手算 `round_up(1000,16)`：(999|15)+1 = 1007+1 = **1008**。
   - 算分类输出 `[1,1000,1,1]` 的 epmat 大小：H=1,W=1 走退化分支 = `1008*2 = 2016` 字节。
3. **需要观察的现象**：理解「255 个通道却按 256 个通道占空间」的浪费从哪来。
4. **预期结果**：浪费比例 = `(256-255)/256 ≈ 0.4%`，对 16 通道分组而言代价极小。
5. 待本地验证：在 `read_forward_result` 的 `printf("epmat_size = 0x%x(%d)")`（L372）处核对打印值是否与手算一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么是向上取整到 16，而不是 4 或 32？

> **答案**：16 是 TPU 数据通路的通道编组宽度（见 u4-l4 输入侧），硬件一次访存搬 16 个通道。软件端 epmat 必须与硬件编组对齐，否则 `epmat2nmat` 的寻址会错位。取 4 太小（不匹配硬件编组），取 32 会过度浪费。

**练习 2**：`h*w*round_up(c,16)*2` 里，能否等价改写成 `(h*w*32) * (round_up(c,16)/16)`？两项各代表什么？

> **答案**：可以。`round_up(c,16)/16` 是「组数」(⌈C/16⌉)，`h*w*32` 是「每组字节数」(每组有 H×W 个位置，每位置 32 字节)。两者相乘即总字节数，与原式代数等价，但物理含义更清晰。

---

### 4.3 epmat2nmat：16 通道分组的反量化还原

#### 4.3.1 概念说明

`epmat2nmat` 是本讲的「翻译核心」。它做两件事：**寻址**（在 epmat 字节流里找到「第 c 个通道、第 hw 个空间位置」的那 2 个字节）和**反量化**（把 int16 除以 `2^exp` 还原成 float）。前者难在 16 通道分组布局，后者就是一次除法。

为什么要在 CPU 上做反量化？因为 TPU 内部用定点计算（更快更省电），而后处理算子（NMS、topk、画框）习惯用浮点。`exp` 这个定点指数由网络在编译期定死，烤进了 eepnet_config（u3-l1/u3-l3），运行时读出来即可。

#### 4.3.2 核心流程

`epmat2nmat(channel, height, width, epmat, exp)` 的双层循环：

```
ch_group      = ceil(channel/16)              // 总组数
epmat_ch_group= height*width*16               // 每组占多少个 int16 单位（×2 字节）
div_val       = 2^exp
for c in 0..channel-1:                         // 遍历每个真实通道
    curr_ch_group = c / 16                     // 这个通道属于第几组
    pdst = dstmat.channel(c)                   // 输出 Mat 第 c 通道的浮点起点
    epmat_offset = (curr_ch_group*epmat_ch_group + (c%16)) * 2   // 组内起始字节
    for hw in 0..(height*width)-1:            // 遍历该通道的每个空间位置
        tmpval = epmat[epmat_offset+0] | (epmat[epmat_offset+1] << 8)  // 小端 int16
        *pdst++ = (float)tmpval / div_val      // 反量化
        epmat_offset += 32                     // 跳到下一个空间位置（同通道）
```

**寻址公式的几何含义**：在一个 16 通道组内，数据按 `[空间位置][16 通道]` 排列，每个空间位置 32 字节。所以「第 c 通道、第 hw 位置」的字节偏移是：

\[ o(c, hw) = \underbrace{\Big(\lfloor c/16 \rfloor \cdot (H \cdot W \cdot 16)\Big) \cdot 2}_{\text{组起始字节}} \;+\; \underbrace{(c \bmod 16) \cdot 2}_{\text{组内通道偏移}} \;+\; \underbrace{hw \cdot 32}_{\text{空间步长}} \]

- `curr_ch_group = c/16`：定位**第几个 16 通道组**。
- `(c%16)*2`：在该组内、当前空间位置的 32 字节槽里，定位**第 c%16 个通道**的 2 个字节。
- `+= 32`：同通道、下一个空间位置——因为同一组的下一个位置正好隔 32 字节。

**反量化公式**（从 epmat 还原一个 float）：

\[ f = \frac{\text{int16}\big(\text{epmat}[o] \;\mid\; (\text{epmat}[o+1] \ll 8)\big)}{2^{\text{exp}}} \]

其中 `int16(...)` 表示把拼出的 16 位值按**有符号**短整型解释（C 里 `short tmpval` 默认有符号），`<<8` 是把高字节左移 8 位，`|` 拼成小端 int16。

#### 4.3.3 源码精读

主函数 `epmat2nmat`：

[eeptpu_sa.cpp:330-361](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L330-L361) — `if(epmat==NULL)` 防御 → `height==1 && channel==1` 走简化分支 → 否则建 `ncnn::Mat(w,h,c)` 双层循环按 16 通道组寻址并反量化。

关键片段（精简）：

```cpp
int ch_group = channel / 16;
if (channel % 16) ch_group++;                 // ceil
int epmat_ch_group = height * width * 16;
int div_val = pow(2, exp);
for (int c=0; c < channel; c++) {
    int curr_ch_group = c / 16;
    float* pdst = (float*)dstmat.channel(c).data;
    int epmat_offset = (curr_ch_group * epmat_ch_group + (c % 16)) * 2;
    for (int hw=0; hw < height*width; hw++) {
        short tmpval = *(epmat + epmat_offset + 0);
        tmpval |= ((*(epmat + epmat_offset + 1)) << 8);   // 小端拼 int16
        *pdst++ = (float)tmpval / div_val;                // 反量化
        epmat_offset += 32;                               // 下一空间位置
    }
}
```

简化分支 `epmat2nmat_simple`：

[eeptpu_sa.cpp:314-329](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L314-L329) — 当 `height==1 && channel==1`（即 `[1,1,1,W]` 的纯一维向量输出）时，建 `ncnn::Mat(width)`，连续读 `width` 个 int16 并逐个除以 `2^exp`。这是一条不涉及 16 通道分组的快路径。

> 注意：常见的分类输出 `[1,C,1,1]`（C>1，如 1000）走的是**主分支**而非简化分支（条件是 `channel==1`，分类的 channel 是 C≠1）。在主分支里 H=W=1 时，`epmat_offset=(c/16*16 + c%16)*2 = c*2`，内层 `hw` 循环只跑一次，等价于按通道线性读取——所以分类也能正确工作。

#### 4.3.4 代码实践（本讲核心任务）

> 这是本讲规格指定的核心实践任务。

1. **实践目标**：讲清 `curr_ch_group=c/16` 与 `epmat_offset=(...+c%16)*2` 的含义，并写出还原公式。
2. **操作步骤**：
   - 对照 `epmat2nmat` 源码，按下面的「寻址分解表」填空。
   - 用 yolov4-tiny 的 out0 `[1,255,13,13]`（exp=8）做样例：`channel=255, height=13, width=13`，故 `ch_group=16`、`epmat_ch_group=13*13*16=2704`、`div_val=2^8=256`。
3. **需要观察的现象**：通道 `c=20` 时，它落在第几组、组内第几槽。
4. **预期结果**：
   - `c=20` → `curr_ch_group = 20/16 = 1`（第 2 组，从 0 计），组内 `c%16 = 4`。
   - 起始字节 `epmat_offset = (1*2704 + 4)*2 = 2708*2 = 5416`，即第 2 组首位置的第 4 个通道。
   - 还原第 `c=20, hw=0` 的浮点值：
     \[ f = \frac{\text{int16}(\text{epmat}[5416] \mid (\text{epmat}[5417]\ll 8))}{256} \]

   **寻址分解表**：

   | 表达式 | 含义 |
   | --- | --- |
   | `curr_ch_group = c/16` | 通道 c 属于第几个 16 通道组 |
   | `epmat_ch_group = H*W*16` | 每组占多少个 int16 单位（×2 字节/单位） |
   | `curr_ch_group * epmat_ch_group` | 跳过前面若干组，定位到本组起始 |
   | `+ (c%16)` | 在本组当前空间位置的 32 字节槽里，选第 c%16 个通道 |
   | `* 2` | 把「int16 单位」换算成字节 |
   | `+= 32`（内层） | 同通道、下一个空间位置（相邻位置隔 32 字节） |

5. 若无硬件：标注「待本地验证」，但寻址推导与公式可在纸面完成。

#### 4.3.5 小练习与答案

**练习 1**：如果误把 `epmat_offset += 32` 改成 `+= 2`，会发生什么？

> **答案**：内层循环不再跨空间位置，而是在同一空间位置的 32 字节内逐 int16 读，相当于把「16 个通道」误当成「16 个位置」。结果是把一个位置的 16 通道值当作 16 个位置的同一通道值，整张特征图会完全错位。说明 32 不是任意数字，而是「一个空间位置的 16 通道槽位宽度」。

**练习 2**：`div_val = pow(2, exp)` 用的是 `pow`（浮点幂），为什么不用 `1 << exp`？

> **答案**：两者在 exp 较小时等价，`1<<exp` 更快。这里用 `pow(2,exp)` 可能是为兼容 exp 较大或可读性；由于 `div_val` 在循环外只算一次，性能影响可忽略。`exp` 典型值 8/12，`1<<8=256` 与 `pow(2,8)=256.0` 结果一致。

**练习 3**：反量化为何是「除以 2^exp」而不是「乘以某个 scale」？

> **答案**：硬件定点数把浮点 x 编码为 `round(x * 2^exp)` 存成 int16，所以还原就是 `int16 / 2^exp`。`exp` 是编译器根据该层数值范围选定的「小数位」，使 int16 动态范围刚好覆盖该层激活值。这与输入侧 `y=round((x-mean)*norm*2^exp)`（u4-l4）是同一套定点思想的对称应用。

---

### 4.4 ncnn::Mat 输出：自实现容器与对齐内存

#### 4.4.1 概念说明

`epmat2nmat` 的产物是 `ncnn::Mat`。裸机工程没有标准 ncnn 库，所以在 `nmat.h` 里塞了一个**最小实现**：一个三维张量（w/h/c）、引用计数、对齐分配。后处理算子（`get_topk`、`yolo3_detection_output_forward`）都拿它当输入。

理解 `ncnn::Mat` 的关键是 `channel(c)`：它返回第 c 个通道的**视图**（一段连续浮点内存的起点），而 `cstep` 决定相邻通道起点之间隔多远——并且 `cstep` 被对齐到了 16 字节边界。

#### 4.4.2 核心流程

`epmat2nmat` 里这样用它：

```cpp
ncnn::Mat dstmat = ncnn::Mat(width, height, channel);   // 建 3D Mat，elemsize=4(float)
...
float* pdst = (float*)dstmat.channel(c).data;           // 取第 c 通道起点
...
*pdst++ = (float)tmpval / div_val;                      // 逐元素写
```

`Mat(w,h,c)` 内部：`dims=3`，`cstep = alignSize(w*h*elemsize, 16)/elemsize`，再用 `fastMalloc` 分配 `cstep*c*elemsize`（对齐到 16 字节）的内存。

`channel(c)` 返回一个二维视图，其 `data = 原data + cstep*c*elemsize`，即跳过前 c 个通道（每个通道 `cstep` 个元素）。

#### 4.4.3 源码精读

`channel(c)` 的实现——本讲 `epmat2nmat` 写入的入口：

[nmat.h:444-447](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L444-L447) — 返回一个新的二维 `Mat(w,h,...)`，其 `data` 指向 `原data + cstep*c*elemsize`。不拷贝、只改指针，故 `*pdst++` 直接写进原缓冲。

```cpp
inline Mat Mat::channel(int c)
{
    return Mat(w, h, (unsigned char*)data + cstep * c * elemsize, elemsize, allocator);
}
```

`create(w,h,c)` 里 `cstep` 的对齐：

[nmat.h:375-402](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L375-L402) — 三维分支：`cstep = alignSize(w*h*elemsize, 16)/elemsize`，把每通道字节数向上取整到 16 的倍数，再除以 `elemsize` 换回「元素个数」。

对齐分配工具：

[nmat.h:73-86](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L73-L86) — `alignSize(sz,n)=(sz+n-1)&-n`（向上取整到 n 的倍数）；`fastMalloc` 多申请 `sizeof(void*)+MALLOC_ALIGN` 字节，用 `alignPtr` 把返回指针对齐到 `MALLOC_ALIGN=16`，并把真实 `malloc` 指针藏在返回指针前一格，供 `fastFree` 取回。

[nmat.h:31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L31) — `MALLOC_ALIGN` 固定为 16。

> 为什么到处是 16？因为 TPU 与 NEON/向量化访存都偏好 16 字节对齐；`cstep` 对齐到 16 让每个通道的起点都是 16 字节对齐地址，后续后处理算子可安全用向量化加载。

#### 4.4.4 代码实践（计算型）

1. **实践目标**：算出 yolov4-tiny out0 还原后的 `ncnn::Mat` 内存布局。
2. **操作步骤**：
   - out0 还原后 `Mat(13, 13, 255)`，`elemsize=4`。
   - 算 `cstep = alignSize(13*13*4, 16)/4 = alignSize(676,16)/4`。`alignSize(676,16)=(676+15)&-16=688`，`688/4=172`。
   - 总元素 = `cstep*c = 172*255 = 43860`，总字节 `≈ 43860*4 = 175,440`。
3. **需要观察的现象**：每个通道实际只有 `13*13=169` 个有效浮点，但 `cstep=172`，多出 3 个是填充。
4. **预期结果**：`channel(c)` 返回的起点每隔 `172*4=688` 字节，且 688 是 16 的倍数，满足对齐。
5. 待本地验证：在 `epmat2nmat` 末尾打印 `dstmat.cstep` 与 `dstmat.total()` 核对。

#### 4.4.5 小练习与答案

**练习 1**：`channel(c)` 返回的 `Mat` 与原 `Mat` 共享内存吗？改它会影响原 Mat 吗？

> **答案**：共享。`channel(c)` 只构造了一个 `data` 指针偏移后的新 `Mat` 对象，没有拷贝数据，也没有新的 `refcount`（用的是 external data 构造，`refcount=0`）。所以经 `*pdst++` 写入会直接落到原缓冲。这与 `clone()`（深拷贝）不同。

**练习 2**：`fastMalloc` 为什么不直接返回 `malloc` 的结果，而要先 `alignPtr` 对齐？

> **答案**：标准 `malloc` 只保证对齐到 `alignof(max_align_t)`（通常 16），但不保证对齐到向量化指令期望的边界，且不同平台行为不一。`fastMalloc` 手动多分配空间再用 `alignPtr` 强制 16 字节对齐，保证返回地址恒为 16 的倍数，便于 SIMD 访存；同时把原始指针藏在 `adata[-1]`，让 `fastFree` 能找回真正要 `free` 的地址。

---

## 5. 综合实践

**任务**：在纸面完整追踪 yolov4-tiny 的 out0 张量 `[1,255,13,13]`（exp=8），从 DDR 地址到 `ncnn::Mat` 的全过程。

请按顺序回答/计算：

1. `read_forward_result` 从 `addr_out[0].hwaddr` 读取多少字节？（用 `epmat_get_size`）
2. 这块 epmat 一共分成几个 16 通道组？每组多少字节？
3. 写出读取「通道 c=255 的最后一个通道、即第 254 个真实通道索引」在 `hw=0` 时的字节偏移公式并算出数值。注意 c=254。
4. 假设那 2 个字节读出 `0x37 0x02`（小端），写出反量化后的浮点值。
5. 还原后的 `ncnn::Mat` 的 `cstep` 是多少？`channel(254)` 的 `data` 相对 `Mat.data` 偏移多少字节？

**参考答案**：

1. `epmat_get_size(255,13,13) = 13*13*round_up(255,16)*2 = 13*13*256*2 = 86,528` 字节。
2. 组数 = `ceil(255/16)=16`；每组 = `13*13*32 = 5408` 字节；`16*5408 = 86,528` ✓。
3. `c=254`：`curr_ch_group=254/16=15`，`c%16=14`；`epmat_offset=(15*2704 + 14)*2 = (40560+14)*2 = 40574*2 = 81,148`。
4. 小端拼 int16：`0x37 | (0x02<<8) = 0x0237 = 567`；`f = 567 / 2^8 = 567/256 ≈ 2.2148`。
5. `cstep = alignSize(13*13*4,16)/4 = 688/4 = 172`（元素）；`channel(254).data` 偏移 = `cstep*254*elemsize = 172*254*4 = 174,752` 字节。

> 这个练习把「读 DDR → 算 size → 16 通道组寻址 → 小端拼 int16 → 除 2^exp → 写对齐 Mat」整条链路串了起来，做完即掌握本讲。

## 6. 本讲小结

- `read_forward_result` 是 forward 之后的「收尾」：遍历 `addr_out`，对每个输出 `malloc` 临时缓冲 → `eepif.mem_read` 从 DDR 读回 → `epmat2nmat` 转浮点 `ncnn::Mat` → `push_back` → `free`。
- **epmat 是 TPU 原生输出格式**：int16 定点 + 16 通道分组 + 32 字节空间步长，与输入侧打包格式（u4-l4）同源。`epmat_get_size = H*W*round_up(C,16)*2`。
- **16 通道分组寻址**：`curr_ch_group=c/16` 定位组，`(c%16)*2` 定位组内通道，内层 `+=32` 跨空间位置。
- **反量化**：`f = int16(小端2字节) / 2^exp`，`exp` 来自 eepnet_config。
- 还原结果存进自实现的 `ncnn::Mat`，`channel(c)` 返回对齐视图，`cstep` 对齐到 16 字节以利向量化后处理。
- 读回的 `outputs` 直接喂给分类 `get_topk` 或检测 `yolo3_detection_output_forward`，完成推理到结果的闭环。

## 7. 下一步学习建议

本讲把「读输出」讲完了，自然的下一步是**后处理**：

- **u6-l1 分类后处理 topk**：看 `get_topk` 如何对 `outputs[0]`（分类向量）做 partial_sort 取 top5，本讲的 `ncnn::Mat` 正是它的输入。
- **u6-l3 yolo3_detection_output 软件层**：看 `yolo3_detection_output_forward` 如何消费 `outputs`（本讲产出的两个 yolov4-tiny 分支 Mat）做解码与 NMS。
- 若想深挖内存底层：回顾 **u4-l3（EEP_INTERFACE）** 的 `mem_read` 逐字节实现，以及 **u8-l3（simplestl 与 nmat）** 对 `fastMalloc/alignSize` 的工程取舍。
