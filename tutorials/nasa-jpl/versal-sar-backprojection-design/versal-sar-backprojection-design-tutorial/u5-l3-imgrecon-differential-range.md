# 图像重建(一)：像素提取与差分距离

## 1. 本讲目标

本讲进入 SAR 反投影的**核心计算内核** `img_reconstruct_kern`。它是整张 AIE 图里数量最多（默认 224 个）、承担真正成像计算的内核。本讲只讲它的**前段**——拿到目标像素后、还没做相位校正之前的部分。读完本讲你应该能够：

- 说清 `ImgReconstruct` 为什么必须是「类内核」，以及 `m_img` 累加缓冲如何跨脉冲持久存在。
- 解释为什么目标像素在包流里以 `int` 传输，进入内核后要用 `reinterpret_cast` 还原成 `float`。
- 读懂用 `aie::sub / mul_square / sqrt` 对 16 个像素并行计算「差分距离」的 SIMD 代码。
- 说明差分距离如何除以 `RANGE_RES`、再加 `half_size` 偏移，映射成 `rc_in` 缓冲里的采样索引。

> 本讲只覆盖 `img_reconstruct_kern` 的「像素提取 + 差分距离 + 索引映射」三步。后续的相位校正（sincos）、线性插值、累加与 dump 出图分别在 u5-l4、u5-l5 讲解。

## 2. 前置知识

本讲默认你已经学过：

- **u1-l4**：`common.h` 里的规模宏（`PULSES=602`、`RC_SAMPLES=512`、`IMG_SOLVERS=224`）与雷达物理常数（`RANGE_RES`、`INV_RANGE_RES`）。
- **u2-l2 / u2-l3**：AIE 的 buffer / stream / pktstream 端口区别，以及 ADF 图的数据驱动（Kahn）执行模型。
- **u5-l1**：`data_broadcast_kern` 把 slowtime（天线几何）与 RC（距离压缩样本）广播给所有重建内核。
- **u5-l2**：`px_demux_kern` 把每个内核各自的目标像素用**包交换（pktstream）**按 `pkt_id` 分发，每包 1376 个像素、末 beat 置 TLAST。

两个本讲要用到的小概念：

- **SIMD 向量（vector）**：AIE 的向量寄存器一次能装 16 个 `float`，一条向量指令同时处理 16 个数。代码里 `aie::vector<float,16>` 就是这种 16 元素向量。
- **差分距离（differential range）**：某像素到天线的几何距离，减去「场景中心参考距离」`r0` 后的差值。它告诉我们在 `rc_in` 缓冲里该取第几个采样——这是反投影「逐像素定位回波」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | 三个内核的实现。本讲精读其中的 `ImgReconstruct::img_reconstruct_kern` 前段。 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 内核声明。本讲关注 `ImgReconstruct` 类的成员（`m_id`、`m_img`）。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 规模宏与雷达常数，决定 `m_img` 大小与索引映射。 |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | 用 `create_object<ImgReconstruct>(id)` 实例化类内核并传入唯一 id。 |

## 4. 核心概念与源码讲解

### 4.1 ImgReconstruct 类与 m_img 累加缓冲

#### 4.1.1 概念说明

回顾 u5-l2：`px_demux_kern` 把目标像素分发到 224 个重建内核，每个内核只处理其中 1376 个像素。但反投影不是「一个脉冲就成一幅图」——它要把**所有 PULSES 个脉冲**（默认 602）对同一像素的贡献**相干累加**，才能让真实目标同相增强、形成聚焦图像。

这就引出一个需求：每个重建内核必须有一块**跨多次调用（每个脉冲调用一次）持久存在**的存储，用来不断累加图像值。普通的自由函数内核（像 `data_broadcast_kern`、`px_demux_kern`）没有「成员变量」，做不到这一点。所以 `img_reconstruct_kern` 被设计成**类 `ImgReconstruct` 的成员函数**，累加缓冲 `m_img` 和内核身份 `m_id` 都是它的成员。

#### 4.1.2 核心流程

- **构造**：图在 `graph.h` 里用 `create_object<ImgReconstruct>(id)` 构造每个重建内核，传入一个全局唯一的 `id`（详见 u4-l2，`id = 32×子图序号 + i`）。构造函数只保存 `id`。
- **每个脉冲调用一次** `img_reconstruct_kern`：接收本脉冲的 slowtime / RC / 本内核的目标像素，做差分距离、相位校正、插值，把结果 `+=` 到 `m_img` 的对应位置。
- **最后一脉冲**：主机把 RTP 参数 `rtp_dump_img_in` 置 1（见 u3-l5），内核检测到后把累加完成的 `m_img` 打包写出（这部分在 u5-l5 讲）。

```
脉冲 0  ──┐
脉冲 1  ──┤   每次调用: m_img[i] += 本脉冲贡献
脉冲 2  ──┤   (rtp_dump_img_in == 0)
...       │
脉冲601──┤   rtp_dump_img_in == 1 ──> 把 m_img 打包输出
```

#### 4.1.3 源码精读

`ImgReconstruct` 类声明在 [custom_kernels.h:22-42](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L22-L42)，关键成员有两个：

```cpp
class ImgReconstruct {
    public:
        ImgReconstruct(int id);
        void img_reconstruct_kern(...);          // 每脉冲调用一次
        static void registerKernelClass() {
            REGISTER_FUNCTION(ImgReconstruct::img_reconstruct_kern);
        }
    private:
        uint32 m_id;                                                     // 内核唯一身份
        alignas(aie::vector_decl_align) cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS]; // 累加缓冲
};
```

- [custom_kernels.h:38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L38)：`m_id` 是内核身份，**不参与成像计算**，只在最后 dump 时写进输出包元数据，供 PL 包路由器把图像回写到正确的 DDR 偏移（见 u6-l1）。
- [custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)：`m_img` 是累加缓冲，元素个数 `(PULSES*RC_SAMPLES)/IMG_SOLVERS = (602×512)/224 = 1376` 个 `cfloat`（复数）。`alignas(aie::vector_decl_align)` 是对齐要求，方便后续向量加载。
- [custom_kernels.h:32-35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L32-L35)：`REGISTER_FUNCTION` 宏是 ADF 框架要求的，用来登记类内核的入口函数。

构造函数定义在 [backprojection.cc:58-60](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L58-L60)，只做一件事——保存 id：

```cpp
ImgReconstruct::ImgReconstruct(int id)
: m_id(id)
{}
```

`m_img` 的累加发生在内核尾部（u5-l5 详讲），此处先看它的入口签名 [backprojection.cc:62-66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L62-L66)，注意五个端口：

```cpp
void ImgReconstruct::img_reconstruct_kern(
    input_buffer<float, extents<BC_ELEMENTS>>& __restrict slowtime_in,  // in[0] 天线几何
    input_buffer<cfloat, extents<RC_SAMPLES>>& __restrict rc_in,        // in[1] 距离压缩样本
    input_pktstream *px_xyz_in,                                          // in[2] 目标像素(包流)
    output_pktstream *img_out,                                           // out[0] 输出图像
    int rtp_dump_img_in);                                                // in[3] RTP 开关
```

这五个端口与 `graph.h` 里 `connect` 的 `in[0]/in[1]/in[2]/in[3]` 一一对应（见 u4-l1、u4-l3）：`in[0]/in[1]` 接广播的 slowtime/RC，`in[2]` 接 demux 分发的像素包流，`in[3]` 接 RTP。

> 待本地验证：`m_img` 作为成员数组，其初始清零状态在可见的构造函数里没有显式写出。相干累加的正确性依赖它在第一次累加前为复数 0；如要在板上调试累加结果，建议先确认框架对内核对象内存的初始化行为。

#### 4.1.4 代码实践

**实践目标**：确认 `m_img` 的大小与「每核像素数」一致，理解类内核的持久状态。

**操作步骤**：

1. 打开 [common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h)，记下 `PULSES=602`、`RC_SAMPLES=512`、`IMG_SOLVERS = AIE_SWITCHES×IMG_SOLVERS_PER_SWITCH = 7×32 = 224`（[common.h:38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L38)）。
2. 打开 [custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)，手算 `m_img` 的元素数。
3. 对照 [graph.h:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L53) 的 `create_object<ImgReconstruct>(...)`，确认每个内核对象独立持有一份 `m_img`。

**需要观察的现象**：`(602×512)/224 = 1376`，与内核里 `const int SAMPLES` 的取值（[backprojection.cc:68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L68)）完全一致——每核恰好处理 1376 个像素，`m_img` 也恰好 1376 个复数累加槽位，一一对应。

**预期结果**：1376。这也印证了 u1-l4 讲过的整除约束：`(PULSES×RC_SAMPLES)/IMG_SOLVERS` 必须整除，否则 `m_img` 大小与实际像素数不匹配，会静默损坏图像。

#### 4.1.5 小练习与答案

**练习 1**：默认配置下 `m_img` 占多少字节？如果 `AIE_SWITCHES` 改成 1（仅 1 个子图、32 个重建核），`m_img` 会变成多大？

> **答**：`m_img` 有 1376 个 `cfloat`，每个 `cfloat` = 2×`float` = 8 字节，故 `1376×8 = 11008` 字节 ≈ 10.75 KiB。若 `AIE_SWITCHES=1`，则 `IMG_SOLVERS=32`，每核像素数 `(602×512)/32 = 9632`，`m_img` 变为 `9632×8 = 77056` 字节 ≈ 75 KiB——会超出单个 tile 的 32 KB 局部存储（见 u2-l1），所以默认才用 7 个子图把每核像素数摊薄到 1376。

**练习 2**：为什么 `img_reconstruct_kern` 用类成员函数，而 `data_broadcast_kern`、`px_demux_kern` 用自由函数？

> **答**：广播内核和解复用内核都是「无状态」的——每次调用把输入直通/分发出去，不需要记住上一次调用的结果。而重建内核需要把 602 个脉冲的贡献**累加**到同一块缓冲 `m_img`，这块缓冲必须跨调用持久存在；只有类成员变量能做到。同时类还持有 `m_id` 用于输出身份标记，自由函数也无法携带。

---

### 4.2 reinterpret_cast 像素还原

#### 4.2.1 概念说明

回顾 u5-l2：`px_demux_kern` 把目标像素以 `float` 逐个 `writeincr` 写进 `output_pktstream`。但在 AIE 的包交换流（pktstream）里，**线上传输的数据类型按 `int`（32 位整数）对待**——这是包交换硬件的数据宽度约定。也就是说，像素的 32 位浮点位模式，在包流里被当成一个 `int` 搬运。

于是在重建内核这一侧，从 `input_pktstream` 读出来的元素类型是 `int`。要拿到原来的 `float` 值，必须把这块 32 位**位模式原样重解释**回 `float`——这正是 `reinterpret_cast` 的用途。它不改变任何二进制位，只改变编译器如何「读」这些位。

#### 4.2.2 核心流程

```
demux 端 (写):  float px  ──writeincr──> [pktstream, 按 int 传输]
                                          │  32 位浮点位模式
重建端 (读):  int raw = readincr(...)  <──┘
              float px = *reinterpret_cast<float*>(&raw)   # 位模式不变，类型变回 float
```

- 先读 1 个 `uint32` **包头**（demux 端用 `writeHeader` 写的）。
- 接着按每个像素读 3 个 `int`（X、Y、Z），分别 `reinterpret_cast` 还原成 `float`。
- 还原后的 X/Y/Z 存进内核局部数组，供后续 SIMD 向量加载。

#### 4.2.3 源码精读

内核一进来就先把本内核要处理的全部像素读出并缓存 [backprojection.cc:68-87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L68-L87)：

```cpp
const int SAMPLES = (PULSES*RC_SAMPLES)/IMG_SOLVERS;   // = 1376

// 目标像素缓存（注意：源码注释说明包流数据类型是 int）
alignas(aie::vector_decl_align) float x_trgt_pxls[SAMPLES];
alignas(aie::vector_decl_align) float y_trgt_pxls[SAMPLES];
alignas(aie::vector_decl_align) float z_trgt_pxls[SAMPLES];

uint32 header = readincr(px_xyz_in);                    // 先读包头
for(int px_idx=0; px_idx < SAMPLES; px_idx++) chess_prepare_for_pipelining {
    int raw_x = readincr(px_xyz_in);                    // 按 int 读
    int raw_y = readincr(px_xyz_in);
    int raw_z = readincr(px_xyz_in);

    x_trgt_pxls[px_idx] = *reinterpret_cast<float*>(&raw_x);  // 位模式还原
    y_trgt_pxls[px_idx] = *reinterpret_cast<float*>(&raw_y);
    z_trgt_pxls[px_idx] = *reinterpret_cast<float*>(&raw_z);
}
```

逐行说明：

- [backprojection.cc:68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L68)：`SAMPLES=1376`，本内核负责的像素数。
- [backprojection.cc:70-75](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L70-L75)：三组 `float` 缓存，对齐到向量边界，方便下一步用 `load` 一次取 16 个。注释明确写了「目标像素以 int 存储，因为那是 pktswitch 流关联的数据类型」。
- [backprojection.cc:78](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L78)：读取 demux 端写入的 32 位包头。
- [backprojection.cc:79-87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L79-L87)：每个像素读 3 个 `int`，再 `reinterpret_cast<float*>` 把地址解引用，得到原始 `float`。`chess_prepare_for_pipelining` 是给编译器的流水化提示（详见 u5-l5）。

> 为什么不直接读 `float`？因为 `readincr(px_xyz_in)` 的返回类型由端口类型 `input_pktstream` 决定，框架按 `int` 给。强行当 `float` 读会触发类型错误；`reinterpret_cast` 是在两种类型间搬运「相同 32 位位模式」的标准做法。

#### 4.2.4 代码实践

**实践目标**：在 PC 上验证「`int` ↔ `float` 位模式重解释」确实只是换个视角、不改变位。

**操作步骤**（示例代码，可在本地任意 C++ 环境运行，与项目无关）：

```cpp
#include <iostream>
#include <cstring>
int main() {
    float px = -51.2f;
    int raw;
    std::memcpy(&raw, &px, sizeof(int));          // 模拟包流: float 当 int 传
    float restored = *reinterpret_cast<float*>(&raw); // 重建端还原
    std::cout << restored << std::endl;           // 应打印 -51.2
}
```

**需要观察的现象**：`restored` 与原始 `px` 完全相等，证明 32 位位模式在 `int` 和 `float` 之间搬运时没有任何改变。

**预期结果**：打印 `-51.2`。这也解释了为什么 demux 端 `writeincr(px_xyz_out, px_xyz_val, ...)`（[backprojection.cc:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L35)）写 `float`、重建端读 `int` 却能拿到正确数值——两边位模式一致。

#### 4.2.5 小练习与答案

**练习 1**：[backprojection.cc:78](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L78) 读到的第一个 `uint32 header` 是谁写的、起什么作用？

> **答**：由 `px_demux_kern` 的 `writeHeader(px_xyz_out, pkt_type, pkt_id)`（[backprojection.cc:27](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L27)）写入，包含 `pkt_id` 等路由信息，供 `pktsplit` 把包分发到正确内核。重建端读出后丢弃（只 `readincr` 一次，不使用其值），因为包已经到达正确的内核。

**练习 2**：如果不用 `reinterpret_cast`，而是直接 `x_trgt_pxls[px_idx] = (float)raw_x;`，会发生什么？

> **答**：`(float)raw_x` 是**数值转换**——它把整数 `raw_x` 的数值（比如 `1083625111`）转成等值的浮点（`1.0836e9`），而不是把它当成浮点位模式解释。结果完全错误。`reinterpret_cast` 才是「不改位、只换读法」的位模式重解释。

---

### 4.3 aie::sub / mul_square / sqrt 差分距离

#### 4.3.1 概念说明

像素还原后，进入反投影最核心的几何计算：**差分距离**。

对于某个目标像素 \((x, y, z)\) 和当前脉冲的天线位置 \((x_{ant}, y_{ant}, z_{ant})\)，像素到天线的几何距离是：

\[ D = \sqrt{(x_{ant}-x)^2 + (y_{ant}-y)^2 + (z_{ant}-z)^2} \]

而 `rc_in` 缓冲里的距离压缩样本是以「场景中心参考距离 \(r_0\)」为中心排列的：索引 `half_size`（= `RC_SAMPLES/2 = 256`）对应 \(r_0\)，比 \(r_0\) 远的回波落在更大索引、更近的落在更小索引。所以我们关心的不是绝对距离 \(D\)，而是它相对 \(r_0\) 的差值——**差分距离**：

\[ d_{\text{diff}} = D - r_0 \]

再用距离分辨率 `RANGE_RES` 把它换算成采样数，加上 `half_size` 偏移，就得到 `rc_in` 里的索引。回顾 u1-l4：

\[ \text{RANGE\_RES} = \frac{\text{RANGE\_WIDTH}}{\text{RC\_SAMPLES}}, \quad \text{RANGE\_WIDTH} = \frac{C}{2 \cdot \text{RANGE\_FREQ\_STEP}} \approx 101.88\,\text{m} \]

默认 `RANGE_RES ≈ 0.199\,\text{m}`（约 20 cm），`INV_RANGE_RES = 1/RANGE_RES ≈ 5.025\,/\text{m}`。

> 关于因子 2：`RANGE_WIDTH` 里的 `2` 已经把雷达信号的**双程（往返）**特性折算进去，因此把单程几何差分距离 \(d_{\text{diff}}\) 除以 `RANGE_RES` 即可直接得到正确的采样偏移量。本讲不展开推导，只需记住「除以 RANGE_RES 把米换算成采样数」。

#### 4.3.2 核心流程

整个差分距离 + 索引映射对 **16 个像素并行**完成（一条向量指令处理 16 个 `float`）：

```
加载 16 个 X/Y/Z 像素 ──> x_pxls_vec, y_pxls_vec, z_pxls_vec   (各 16 元素向量)

# 1) 差分距离 (backprojection.cc:121-135)
dx = x_ant - x_pxls_vec      ; dy = y_ant - y_pxls_vec      ; dz = z_ant - z_pxls_vec
D  = sqrt(dx² + dy² + dz²)                                # aie::mul_square + add + sqrt
differ_range = D - r0

# 2) 索引映射 (backprojection.cc:139-147)
px_idx        = INV_RANGE_RES * differ_range + half_size    # 米 -> 采样数, 再居中
low_idx       = trunc_to_int(px_idx - 0.5)                  # 四舍五入到最近整数
high_idx      = low_idx + 1                                 # 供后续线性插值(u5-l5)
```

数学上，索引映射公式是：

\[ \text{idx} = \frac{d_{\text{diff}}}{\text{RANGE\_RES}} + \text{half\_size} \]

其中 `half_size = 256`，保证 \(d_{\text{diff}}=0\)（像素恰在场景中心距离）时索引落在缓冲正中。

#### 4.3.3 源码精读

**先取天线几何参数** [backprojection.cc:93-101](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L93-L101)：

```cpp
int half_size = RC_SAMPLES/2;                       // = 256
auto st_in_vec_iter = aie::begin_vector<BC_ELEMENTS>(slowtime_in);
auto st_in_vec = *st_in_vec_iter++;
float x_ant = st_in_vec[0];                         // 天线 X
float y_ant = st_in_vec[1];                         // 天线 Y
float z_ant = st_in_vec[2];                         // 天线 Z
float r0    = st_in_vec[3];                         // 场景中心参考距离
```

slowtime 是 `data_broadcast_kern` 广播来的 4 个 `float`（即 `BC_ELEMENTS=4`，见 u5-l1），一次向量读取全部取出。

**主循环逐 16 像素处理** [backprojection.cc:111-135](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L111-L135)：

```cpp
for(int px_seg_idx=0; px_seg_idx < SAMPLES/16; px_seg_idx++) chess_prepare_for_pipelining {
    // 一次加载 16 个像素的 X/Y/Z
    x_pxls_vec.load(x_trgt_pxls + px_seg_idx*16);
    y_pxls_vec.load(y_trgt_pxls + px_seg_idx*16);
    z_pxls_vec.load(z_trgt_pxls + px_seg_idx*16);

    /**** 计算差分距离 ****/
    auto x_vec = aie::sub(x_ant, x_pxls_vec);            // x_ant - x (16 路)
    auto x_diff_sq_acc = aie::mul_square(x_vec);          // (x_ant - x)^2
    auto y_vec = aie::sub(y_ant, y_pxls_vec);
    auto y_diff_sq_acc = aie::mul_square(y_vec);
    auto z_vec = aie::sub(z_ant, z_pxls_vec);
    auto z_diff_sq_acc = aie::mul_square(z_vec);

    auto xy_add_acc  = aie::add(x_diff_sq_acc, y_diff_sq_acc.to_vector<float>(0));
    auto xyz_add_acc = aie::add(xy_add_acc, z_diff_sq_acc.to_vector<float>(0));
    auto xyz_sqrt_acc = aie::sqrt(xyz_add_acc);           // D = sqrt(dx²+dy²+dz²)

    auto differ_range_vec = aie::sub(xyz_sqrt_acc, r0).to_vector<float>(0);  // D - r0
```

- 循环跑 `SAMPLES/16 = 1376/16 = 86` 次，每次处理 16 个像素。
- `aie::sub(scalar, vec)` 把标量从向量逐元素减去；`aie::mul_square(vec)` 逐元素平方。这些都会编译成单条 SIMD 指令。
- `.to_vector<float>(0)` 是把累加器（accumulator）转回普通 `float` 向量，方便后续标量运算。`xyz_sqrt_acc` 即距离 \(D\)；减去 `r0` 得 `differ_range_vec`（差分距离）。

**索引映射** [backprojection.cc:137-147](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L137-L147)：

```cpp
// 把差分距离换算成 rc_in 采样索引
auto px_idx_acc = aie::mul(INV_RANGE_RES, differ_range_vec);   // d_diff / RANGE_RES
px_idx_acc = aie::add(px_idx_acc, (float)(half_size));         // + 256 居中

// 四舍五入到最近整数：减 0.5 再截断
auto low_idx_float_vec = aie::sub(px_idx_acc, 0.5f).to_vector<float>(0);
auto low_idx_int_vec   = aie::to_fixed<int32>(low_idx_float_vec);   // 截断到 int
auto high_idx_int_vec  = aie::add(low_idx_int_vec, 1);              // +1 供插值
```

- [backprojection.cc:139](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L139)：`INV_RANGE_RES` 来自 [common.h:58](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L58)，乘法实现「除以 RANGE_RES」（乘倒数比做除法快）。
- [backprojection.cc:142](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L142)：加 `half_size=256`，把「相对 r0 的偏移」对齐到缓冲中心。
- [backprojection.cc:145-147](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L145-L147)：`to_fixed<int32>` 是**向零截断**（C 风格）。先减 0.5 再向零截断 ≈ 四舍五入（正数完全等价于 round-half-down）。得到的 `low_idx` 与 `low_idx+1 = high_idx` 是后续线性插值要用的两个相邻采样索引（u5-l5 详讲）。

> 这段索引映射产出的 `low_idx_int_vec / high_idx_int_vec` 是 16 路 SIMD 算出来的，但**下一步用它们去索引 `rc_in` 时却只能逐元素标量访问**——这是整个算法的性能瓶颈，u5-l5 会专门讨论。

#### 4.3.4 代码实践

**实践目标**：用 NumPy 复现「16 个像素的差分距离 + 索引映射」，对照源码理解 `INV_RANGE_RES` 与 `half_size` 偏移如何把距离映射到 `rc_in` 索引。

**操作步骤**（示例代码，可在本地 Python 环境运行；常量取自 `common.h`）：

```python
# 示例代码：复现 backprojection.cc:118-147 的差分距离与索引映射
import numpy as np

# ---- 取自 common.h 的项目常数 ----
RC_SAMPLES      = 512
HALF_SIZE       = RC_SAMPLES // 2                       # = 256  (backprojection.cc:93)
C               = 299792458.0
RANGE_FREQ_STEP = 1471301.6
RANGE_WIDTH     = C / (2.0 * RANGE_FREQ_STEP)           # ≈ 101.88 m  (common.h:56)
RANGE_RES       = RANGE_WIDTH / RC_SAMPLES              # ≈ 0.199 m   (common.h:57)
INV_RANGE_RES   = 1.0 / RANGE_RES                       # ≈ 5.025 /m  (common.h:58)
print(f"RANGE_RES={RANGE_RES:.4f} m, INV_RANGE_RES={INV_RANGE_RES:.4f} /m, half_size={HALF_SIZE}")

# ---- 一个脉冲的 slowtime：天线位置 + 场景中心参考距离 ----
x_ant, y_ant, z_ant, r0 = 100.0, 50.0, 30.0, 120.0     # 对应 backprojection.cc:98-101

# ---- 16 个目标像素 (X 按距离网格取，Y/Z 取 0，对应 graph.cpp:161-167 的网格生成) ----
rng_idx = np.arange(16)
x_pxls = (rng_idx - HALF_SIZE) * RANGE_RES              # 像素 X 坐标
y_pxls = np.zeros(16)
z_pxls = np.zeros(16)

# ---- 1) 差分距离 (对应 backprojection.cc:121-135) ----
D = np.sqrt((x_ant - x_pxls)**2 +
            (y_ant - y_pxls)**2 +
            (z_ant - z_pxls)**2)
differ_range = D - r0

# ---- 2) 索引映射 (对应 backprojection.cc:139-147) ----
px_idx  = INV_RANGE_RES * differ_range + HALF_SIZE      # 米 -> 采样数, 居中
low_idx = (px_idx - 0.5).astype(np.int32)               # 向零截断 ≈ to_fixed<int32>
high_idx = low_idx + 1

for i in range(16):
    print(f"px{i:2d}  X={x_pxls[i]:+7.3f}m  d_diff={differ_range[i]:+7.3f}m  "
          f"-> idx={px_idx[i]:7.2f}  low={low_idx[i]}  high={high_idx[i]}")
```

**需要观察的现象**：

1. `RANGE_RES` 应约 0.199 m，`INV_RANGE_RES` 约 5.025。
2. 像素 X 越接近天线 X（本例里天线 X=100，而像素 X 在 -51m 附近，所以都偏远），`differ_range` 越大，索引越大；像素恰在 \(r_0\) 距离时 `differ_range≈0`，索引落在 256 附近。
3. 所有 `low_idx` 都应落在 `[0, RC_SAMPLES-1] = [0, 511]` 内——若超出，说明该像素的回波不在 `rc_in` 缓冲范围内（图像边缘常见，后续插值会处理）。
4. `high_idx = low_idx + 1` 始终成立，为线性插值准备。

**预期结果**：以本例参数，`differ_range` 大致在 30–45 m 区间，`px_idx` 大致落在 410–480 区间，`low_idx/high_idx` 均为合法采样索引。改大 `r0`（场景中心更远）会让整体索引向 256（中心）靠拢；把 `INV_RANGE_RES` 设错（比如忘掉 `1/`）会让索引爆炸性变大——这正是为什么要用倒数预先算好 `INV_RANGE_RES`。

**待本地验证**：本例的天线位置与 `r0` 是为演示构造的占位值；真实 GOTCHA 数值需从 `gotcha_slowtime_*.csv` 读取后才有意义，但**计算流程与索引范围规律**与板上完全一致。

#### 4.3.5 小练习与答案

**练习 1**：差分距离为什么要减去 `r0`？如果不减，索引公式要怎么改？

> **答**：`rc_in` 的样本是围绕场景中心参考距离 `r0` 排列的，缓冲中心（索引 `half_size=256`）对应 `r0`。减去 `r0` 把绝对距离转成「相对场景中心的差」，这样差为 0 时索引自然落在缓冲中心。若不减 `r0`，就得用绝对距离 \(D\) 除以 `RANGE_RES` 再减去一个与 `r0` 对应的基准偏移，本质等价但不够直观，且 `r0` 每脉冲变化时不便处理。

**练习 2**：默认配置下 `half_size` 是多少？把 `RC_SAMPLES` 改成 256 后它会变成多少，对索引映射有什么影响？

> **答**：默认 `half_size = RC_SAMPLES/2 = 256`。若 `RC_SAMPLES=256`，则 `half_size=128`，同时 `RANGE_RES` 会变大一倍（因为 `RANGE_WIDTH` 不变、样本数减半）。索引映射 `idx = d_diff/RANGE_RES + half_size` 会同时受这两个量影响——`half_size` 保证缓冲中心仍对应 `r0`。

**练习 3**：`low_idx` 用「减 0.5 再 `to_fixed<int32>`」实现，它和直接 `round()` 有何细微差别？

> **答**：`to_fixed<int32>` 是**向零截断**（truncation toward zero），对正数等价于 `floor`，对负数则向 0 靠拢。因此「减 0.5 再截断」对正数是标准的四舍五入（半值向下），但对负数在 `.5` 边界会与 `round()` 略有不同。由于合法采样索引通常为正，实际影响很小；但若调试时遇到负索引边缘像素，需注意这个不对称。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「手工走一遍重建内核前段」的练习。

**任务**：选定 1 个重建内核（默认配置下它负责 1376 个像素中的某一段），在纸上/Python 里追踪它的前段数据流，回答下列问题。

1. **像素从哪来、怎么还原**：根据 u5-l2，这个内核的 1376 个像素由 `px_demux_kern` 经 `pktsplit<32>` 按 `pkt_id` 分发。写出重建内核读入这 1376 个像素时的循环次数（含包头共读了多少次 `readincr`），并说明每个像素的 X/Y/Z 经过了 `int → float` 的 `reinterpret_cast`。
2. **累加缓冲大小**：确认该内核的 `m_img` 有 1376 个 `cfloat`，并解释为什么它必须是类成员（跨 602 个脉冲累加）。
3. **差分距离与索引**：用 4.3.4 的 NumPy 代码，把天线位置换成「天线在场景上方 500m 处」的合理设定（例如 `x_ant=0, y_ant=0, z_ant=500, r0=500`），观察此时像素的 `differ_range` 与索引分布。解释为什么此时多数索引会落在 `half_size=256` 附近。
4. **性能预判**：差分距离这段（[backprojection.cc:121-135](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L121-L135)）是 16 路 SIMD，而紧随其后的 `rc_in` 取值（[backprojection.cc:178-185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185)）却退化为逐元素标量循环。结合本讲所学，初步说明这是为什么（动态索引无法向量化），为 u5-l5 的性能讨论做铺垫。

**预期产出**：一张数据流图（像素包流 → 还原 → 差分距离 → 索引）加一组自算的索引数值。

## 6. 本讲小结

- `img_reconstruct_kern` 是**类 `ImgReconstruct` 的成员函数**，因为反投影要把 602 个脉冲的贡献累加到持久的 `m_img` 缓冲（1376 个 `cfloat`），自由函数做不到跨调用保持状态。
- 目标像素在包交换流（pktstream）里按 `int` 传输，重建端必须 `reinterpret_cast<float*>` 把 32 位位模式原样还原成 `float`；这与 demux 端以 `float` 写入不矛盾，因为位模式一致。
- 差分距离 \(d_{\text{diff}}=\sqrt{(x_{ant}-x)^2+(y_{ant}-y)^2+(z_{ant}-z)^2}-r_0\) 用 `aie::sub/mul_square/sqrt` 对 **16 个像素并行**计算，是 SIMD 性能优势最明显的一段。
- 索引映射 `idx = d_diff × INV_RANGE_RES + half_size` 把差分距离换算成 `rc_in` 采样索引，`half_size=256` 保证 \(d_{\text{diff}}=0\) 时索引居中；减 0.5 再 `to_fixed<int32>` 实现近似四舍五入，得到供插值的 `low_idx / high_idx`。
- 本讲止步于「算出索引」；用索引去 `rc_in` 取值、做相位校正与线性插值、累加到 `m_img`，是 u5-l4、u5-l5 的内容。

## 7. 下一步学习建议

- **u5-l4（相位校正与 sincos）**：紧接 [backprojection.cc:150-171](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L150-L171)，讲本讲算出的 `differ_range_vec` 如何乘 `ph_corr_coef` 得到相位角，再折叠到 \([-\pi,\pi]\) 交给 `sincos_complex`。
- **u5-l5（插值、累加与流水）**：讲 [backprojection.cc:177-213](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L177-L213)，用本讲算出的 `low_idx/high_idx` 做线性插值、累加到 `m_img`，以及为何此处退化为标量循环（性能瓶颈）。
- **延伸阅读**：对照 [doc/sections/implementation.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex) 中「Image Reconstruction Kernel」一节，看官方文档如何用自然语言描述本讲这三步算法。
