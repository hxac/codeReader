# EEPTPU 运行库 API 与 classify demo

## 1. 本讲目标

本讲是 SDK 路线里第一篇「真正跑通一次推理」的讲义。读完本讲后，你应该能够：

- 说出 `libeeptpu_pub` 提供的 `EEPTPU` 类的**典型调用顺序**：`init → set_base_address → set_interface → load_bin → set_input → forward → 读结果 → close`。
- 看懂 `input_shape`（NCHW 四元组）和 `EEPTPU_RESULT`（含 `data` 与 `shape`）这两个核心数据结构，并能据此把图像喂进网络、把输出解析成分类得分。
- 用 `compile.sh` 在 x86 主机上**交叉编译**出能在 ZynqMP 板卡（ARM64）上运行的 `demo` 可执行文件，再用 `test.sh` 在板上跑出 top5 分类结果。

本讲只讲 **Linux 路线** 下的 `classify`（图像分类）demo。裸机（standalone）路线下的寄存器协议会在 U4 单元单独讲，本讲不展开。

## 2. 前置知识

在进入源码前，先建立三个直觉。它们都来自前置讲义，这里只做最短的承接：

1. **两条部署路线**（来自 u2-l1）：SDK 提供 Linux demo（高层 API，跨 ARM32/ARM64/x86）和裸机 standalone（直接驱动寄存器）两条路线。本讲走第一条。无论哪条路线，前提都是先用 `eeptpu_compiler` 把模型编译成 `*.pub.bin`，bin 里打包了权重、调度表、地址表和输入输出 shape。

2. **两个魔法地址**（来自 u1-l3 / u1-l4）：TPU 在 ZynqMP 上有两条 AXI 通路——**控制通路**（PS 写 TPU 寄存器，落在物理地址 `0xA0000000`）和**数据通路**（TPU 经 HP 口访问 DDR 搬张量）。这两个地址由 Vivado 工程的 `assign_bd_address` 定死，软件必须照抄。本讲你会看到它们再次出现。

3. **图像怎么来**（来自 u2-l2）：所有 demo 共用 `eepimg` 库读图、缩放、画框。读出来的是 `image_bytes` 结构（`w/h/c/data/layout`），喂给网络前要按 `input_shape` 缩放到固定尺寸。本讲会调用 `eepimg_load_image` / `eepimg_resize`，但不再重复讲它们的内部实现。

> 还需要知道一个事实：`EEPTPU` 类的真正声明在头文件 `eeptpu.h` 里，而该头文件**随闭源的 `libeeptpu_pub` 库分发**（编译时由 `../libs/${pf}/eep/include` 提供，不在本仓库内）。因此本讲**不臆造头文件里的字段定义**，所有 API 描述都严格依据 `main.cpp` 里对它们的**真实调用**——这些调用本身就是最可靠的「接口使用说明书」。

## 3. 本讲源码地图

本讲只涉及 `classify` demo 的三个文件，加上一个不在仓库内的库头文件：

| 文件 | 作用 | 是否在仓库内 |
| --- | --- | --- |
| `sdk/demo/classify/main.cpp` | demo 主体：初始化、读图、推理、top5 后处理 | ✅ 可读源码 |
| `sdk/demo/classify/compile.sh` | 交叉编译脚本：按平台切换编译器，链接 `libeeptpu_pub` | ✅ 可读源码 |
| `sdk/demo/classify/test.sh` | 板上运行脚本：指定 bin 与测试图，`sudo` 跑 `demo` | ✅ 可读源码 |
| `eeptpu.h`（来自 `libeeptpu_pub`） | `EEPTPU` 类、`EEPTPU_RESULT`、`EEPTPU_REG_ZONE` 等声明 | ❌ 闭源库附带 |

此外，`main.cpp` 还 `#include` 了 `eep_image.h`（u2-l2 讲过）和 `npy_load.h`（npy 加载，本讲 classify 没直接用，但因链接了 `cnpy.cpp` 而保留）。这两者不是本讲重点。

## 4. 核心概念与源码讲解

### 4.1 eeptpu_init 初始化流程

#### 4.1.1 概念说明

「初始化」要回答一个问题：**软件怎么和板卡上的 TPU 对上话？**

答案是一组配置：告诉库「走哪种接口（SoC 内存映射，还是 PCIE/XDMA）」「TPU 的寄存器在哪个物理地址」「TPU 读写张量数据的内存在哪个基地址」。这三件事配齐，库才能在后续 `forward` 时正确地往寄存器写命令、从 DDR 读写数据。

classify demo 把这套配置封装在一个本地函数 `eeptpu_init(int interface_type, char* path_bin)` 里，整个进程只调用一次。

#### 4.1.2 核心流程

`eeptpu_init` 的伪代码如下：

```
1. 若 tpu 还是 NULL，用 tpu->init() 拿到真正的 EEPTPU 对象（库提供的工厂入口）
2. 按 interface_type 分两条分支：
     PCIE 分支：
       - set_interface_info_pcie(三个 /dev/xdma0_* 设备文件)
       - set_tpu_mem_base_addr(0x0)
       - 注册 reg_zone：{core_id=0, addr=0x00040000, size=256KB}
       - set_base_address(0,0,0,0)
     SoC 分支（默认，arm64）：
       - 注册 reg_zone：{core_id=0, addr=0xA0000000, size=0x1000}
       - set_base_address(0x40000000 ×4)
3. set_interface(interface_type)            # 真正生效
4. 打印 库版本 / 硬件版本 / 硬件信息          # 自检
5. load_bin(path_bin)                       # 加载编译好的网络
6. 返回 0
```

这里有两个关键概念：

- **接口类型 `eepInterfaceType`**：`eepInterfaceType_SOC`（默认）表示 TPU 寄存器被映射到 PS 的物理地址空间，CPU 直接读写那段地址就等于操作寄存器；`eepInterfaceType_PCIE` 表示主机经 PCIE 卡上的 XDMA 引擎访问 TPU，要用 `/dev/xdma0_user`（控制）、`/dev/xdma0_h2c_0`（host→card）、`/dev/xdma0_c2h_0`（card→host）三个设备文件。
- **寄存器 zone（`EEPTPU_REG_ZONE`）**：一个 `{core_id, addr, size}` 三元组，描述「第几个核的寄存器，在哪个物理地址，多大」。`core_id=0` 是第一个 TPU 核。`addr=0xA0000000` 正是 u1-l4 里 `assign_bd_address` 给 TPU 控制通路分配的地址——软硬件契约在这里对上。

#### 4.1.3 源码精读

先看全局对象与默认接口选择：demo 用一个全局指针 `tpu` 持有 EEPTPU 对象，默认走 SoC 接口。

[main.cpp:L15-L18](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L15-L18) — 全局 `EEPTPU *tpu = NULL;` 与默认 `eepInterfaceType_SOC`，注释里保留了切到 PCIE 的写法。

`eeptpu_init` 的开头拿到真正的对象：`tpu = tpu->init()` 是库提供的**工厂入口**，返回新建的 `EEPTPU*`。

[main.cpp:L29-L34](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L29-L34) — `eeptpu_init` 入参是接口类型与 bin 路径；`if (tpu == NULL) tpu = tpu->init();` 拿到对象。（此刻 `tpu` 还是 NULL，这里能工作是因为 `init()` 作为工厂不访问 `this`，是 demo 的固定写法。）

接下来是 **SoC / arm64 分支**——这是 ZynqMP 板卡默认走的路径：

[main.cpp:L51-L65](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L51-L65) — SoC 分支：注册 `core_id=0`、`addr=0xA0000000`、`size=0x1000` 的 reg zone，并把数据基地址设为 `0x40000000`。

要点解读：

- `0xA0000000` 是 TPU **寄存器**地址（控制通路），与 u1-l4 的硬件设计一致；`size=0x1000`（4KB）够放控制寄存器。
- 注释掉的那行 `core_id=1 / 0xA0040000` 是**多核**配置预留——第二个核的寄存器区紧跟在后面。要跑双核 demo 时把它打开（u7-l1 会用到）。
- `eeptpu_set_base_address(0x40000000,0x40000000,0x40000000,0x40000000)` 设置的是 TPU **数据内存基地址**（数据通路），4 个相同参数对应 4 组基地址寄存器（`0x40000000` = 1GB 偏移，落在 DDR 低 2GB 可达区域，见 u1-l4）。
- 再看 `#else` 的 arm32 分支：寄存器地址变成 `0x43C00000`、数据基地址变成 `0x30000000`——**32 位与 64 位地址空间不同**，所以基地址也不同。这正是 u2-l4 要展开讨论的话题，本讲先记住「换平台要换地址」。

最后三步是通用的：真正生效、打印版本自检、加载 bin。

[main.cpp:L74-L89](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L74-L89) — `eeptpu_set_interface` 让配置生效；三行 `printf` 打印库/硬件版本做自检；`eeptpu_load_bin(path_bin)` 加载网络。

#### 4.1.4 代码实践

**实践目标**：看清 SoC 与 PCIE 两条分支配置了哪些不同的参数。

**操作步骤**：

1. 打开 `sdk/demo/classify/main.cpp`，定位到 `eeptpu_init` 函数（约 29–90 行）。
2. 准备一张两列对照表：左列 SoC，右列 PCIE，逐项填入「mem_base_addr」「reg_zone 的 addr 与 size」「base_address 四个参数」「用到的设备文件」。

**需要观察的现象 / 预期结果**：

- SoC 分支**没有**调用 `eeptpu_set_interface_info_pcie`，也**没有**显式 `set_tpu_mem_base_addr`，而是用 `set_base_address(0x40000000×4)` 直接给数据基地址。
- PCIE 分支多出三个 `/dev/xdma0_*` 设备文件，并把 mem_base 设为 `0x0`、reg_zone 地址设为 `0x00040000`（与 SoC 的 `0xA0000000` 完全不同，因为 PCIE 用的是卡上的 BAR 地址，不是 CPU 物理地址）。

> 命令运行结果：待本地验证（需要 PCIE 卡或板卡环境）。本实践以**源码阅读 + 填表**为主，不需要真正上板。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SoC 分支里 reg_zone 的 `addr` 用 `0xA0000000`，而 PCIE 分支用 `0x00040000`？

> **答案**：SoC 模式下 TPU 寄存器被映射到 PS 的物理地址空间，`0xA0000000` 是硬件设计（`assign_bd_address`）给 TPU 控制通路分配的物理地址；PCIE 模式下主机经 XDMA 访问 PCIE 卡，`0x00040000` 是卡上 BAR 空间里寄存器区的偏移。两者是不同地址体系，不能混用。

**练习 2**：把 `static int eep_interface_type` 那行的取值从 `eepInterfaceType_SOC` 换成 `eepInterfaceType_PCIE`，程序会在哪一步表现不同？

> **答案**：会走进 `eeptpu_init` 的 PCIE 分支，多执行 `eeptpu_set_interface_info_pcie` 打开三个 `/dev/xdma0_*` 设备；若主机上没有这些设备（或没有 PCIE 卡），该调用会返回负数，`eeptpu_init` 提前返回错误。

---

### 4.2 load_bin 与 set_input

#### 4.2.1 概念说明

初始化之后有两件事要做：

1. **加载网络**（`load_bin`）：把 `eeptpu_compiler` 编译出的 `*.pub.bin` 读进来。bin 里有权重、调度表、地址表，**还有输入输出的 shape**。加载成功后，`tpu->input_shape[]` 就被填充好了，程序据此知道该喂多大的图。
2. **写入输入**（`set_input`）：把一张预处理好的图像数据送进 TPU 的输入缓冲区。对分类网络，输入通常是固定尺寸（如 224×224×3）。

这一节的难点是 `input_shape` 的**维度顺序**：它是一个长度为 4 的数组，按 **NCHW** 排列。

#### 4.2.2 核心流程

classify 的输入写入逻辑封装在 `eeptpu_write_input` 里：

```
1. 读 input_shape[1]（通道数 C）决定按哪种像素序加载：
     C==3 → BGR（EEPIMG_PIXEL_BGR）
     C==1 → 灰度（EEPIMG_PIXEL_GRAY）
2. eepimg_load_image(path, 像素序) → 得到原图 image_bytes
3. eepimg_resize(原图, input_shape[3], input_shape[2])  # 注意顺序是 (W, H)
4. eeptpu_set_input(data, c, h, w, 0)                    # 第 5 个参数是输入索引 0
5. 释放缩放图，把原图返回给调用者（用于后续可视化）
```

`input_shape` 四个下标的含义：

| 下标 | 含义 | 在本 demo 里的用途 |
| --- | --- | --- |
| `input_shape[0]` | N（batch） | 通常为 1 |
| `input_shape[1]` | C（通道数） | 决定按 BGR(3) 还是 GRAY(1) 读图 |
| `input_shape[2]` | H（高） | resize 的目标高 |
| `input_shape[3]` | W（宽） | resize 的目标宽 |

> 易错点：`eepimg_resize` 的参数顺序是 `(img, width, height)`，所以传的是 `(input_shape[3], input_shape[2])` 即 **(W, H)**，下标顺序反过来。这是 NCHW 与「宽在前」图像接口之间常见的坑。

#### 4.2.3 源码精读

[main.cpp:L92-L105](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L92-L105) — 按通道数选择像素序：`input_shape[1]==3` 用 `EEPIMG_PIXEL_BGR`，`==1` 用 `EEPIMG_PIXEL_GRAY`；注释里保留了切到 RGB（darknet 训练的模型）的写法。

[main.cpp:L111-L114](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L111-L114) — `eepimg_resize(img_orig, input_shape[3], input_shape[2])` 缩放到网络输入尺寸，再 `eeptpu_set_input(img.data, img.c, img.h, img.w, 0)` 送入 TPU。

注意 `set_input` 的参数是 `(data, c, h, w, input_index)`——它直接取 `image_bytes` 的 `c/h/w`（已经是缩放后的值），最后一个 `0` 是输入索引（单输入网络固定为 0；多输入见 u7-l2）。set 成功后立刻 `eepimg_free(img)` 释放缩放图副本（u2-l2 强调过成对释放）。

`load_bin` 本身只有一行，但很关键——它在 `eeptpu_init` 末尾：

[main.cpp:L81-L85](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L81-L85) — `tpu->eeptpu_load_bin(path_bin)` 加载网络；失败时打印 `Load bin fail` 并返回错误码。加载成功后 `input_shape` 才可用。

#### 4.2.4 代码实践

**实践目标**：理解 `input_shape` 如何驱动读图与缩放。

**操作步骤**：

1. 在 `main.cpp` 找到 `printf("Network input shape: ...")`（约 146 行），它会把四个值打出来。
2. 假设你用的是 `eeptpu_s2_mobilenet_v1.pub.bin`（MobileNetV1，典型输入 224×224×3），手算四个下标：`[1, 3, 224, 224]`。
3. 追踪：`input_shape[1]=3` → 走 BGR 分支；`resize(img, 224, 224)`；`set_input(data, 3, 224, 224, 0)`。

**需要观察的现象 / 预期结果**：

- 打印应为 `Network input shape: [1,3,224,224]`（具体以你手上的 bin 为准——**待本地验证**）。
- 把 `EEPIMG_PIXEL_BGR` 换成 `EEPIMG_PIXEL_RGB`，分类结果通常会变差（颜色通道与训练时不一致），但不会崩溃——可用于验证「像素序必须与训练一致」。

> 命令运行结果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `eepimg_resize` 传的是 `(input_shape[3], input_shape[2])` 而不是 `(input_shape[2], input_shape[3])`？

> **答案**：`input_shape` 是 NCHW 顺序，`[2]` 是 H、`[3]` 是 W；而 `eepimg_resize` 的签名是 `(img, width, height)`，宽在前。所以要先传 W（`[3]`）再传 H（`[2]`）。顺序写反会导致图像被转置，分类出错。

**练习 2**：`eeptpu_set_input` 最后一个参数 `0` 是什么意思？什么时候不是 0？

> **答案**：它是**输入索引**，表示往第 0 个输入里写数据。单输入网络（如分类）只有一个输入，固定写 0；多输入网络（多个图像或多个张量输入）需要分别往 0、1、2… 写，这部分在 u7-l2 的 `nntpu_test` 里展开。

---

### 4.3 forward 与结果读取

#### 4.3.1 概念说明

输入就绪后，调用 `eeptpu_forward` 让 TPU 真正算一次。算完后，结果不是「一个数」，而是一个 **`EEPTPU_RESULT` 结构的数组**（`std::vector<EEPTPU_RESULT>`），因为一个网络可能有多个输出张量。

对分类网络，通常只有一个输出：形状为 `[1, C, 1, 1]`（C 是类别数，如 1000），里面是每个类的得分。本模块要解决两件事：

1. **怎么读出结果**：理解 `EEPTPU_RESULT` 的 `data` 与 `shape`。
2. **怎么把得分变成 top5**：把一维得分向量做 `partial_sort` 取最大的 5 个，并记住它们的原始下标（即类别 ID）。

此外，demo 还展示了两种**计时**：软件墙钟（`get_current_time`，含往返开销）与硬件纯计算时间（`eeptpu_get_tpu_forward_time`，单位微秒）。

#### 4.3.2 核心流程

main 里 forward 与结果读取的伪代码：

```
st = get_current_time()                          # 软件计时开始
vector<EEPTPU_RESULT> result;
tpu->eeptpu_forward(result)                      # 推理，结果填入 result
et = get_current_time()                          # 软件计时结束（hw+sw）
hwus = tpu->eeptpu_get_tpu_forward_time()        # 硬件纯计算耗时(μs)

topk = min(5, result[0].shape 的总元素数)
get_topk(result[0], topk, top_list)              # 取 top5
打印 top_list（每个元素是 (score, class_id)）
results_release(result)                          # 释放 result[i].data
```

`EEPTPU_RESULT` 的两个字段（依 main.cpp 用法推断）：

| 字段 | 含义 | 用法 |
| --- | --- | --- |
| `data` | 指向结果数据的指针（按 float 读） | `result[i].data[k]` 取第 k 个得分；用完要 `free()` |
| `shape[4]` | 输出的 NCHW 四维形状 | 分类输出常是 `[1, C, 1, 1]`，总元素数 = 四项之积 |

`get_topk` 的算法思路（经典 topk）：

1. 把 `result.data` 拷成一个 `vector<float> cls_scores`（长度 = shape 四项之积）。
2. 构造 `(score, index)` pair 列表——**必须带 index**，因为排序后还要知道得分对应哪个类别。
3. 用 `std::partial_sort` + `std::greater` 把前 `topk` 个最大的挪到队首（只排前 k 个，不必全排序，复杂度 \(O(n \log k)\)）。
4. 取前 `topk` 个填进 `top_list`。

复杂度说明：全排序是 \(O(n \log n)\)，而 `partial_sort` 只要 \(O(n \log k)\)，当类别数 \(n=1000\)、\(k=5\) 时显著更快。

#### 4.3.3 源码精读

先看 forward 调用与双计时：

[main.cpp:L161-L171](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L161-L171) — `get_current_time()` 包住 `eeptpu_forward` 测「hw+sw」总耗时；`eeptpu_get_tpu_forward_time()` 返回硬件微秒数，除以 1000 得毫秒。

再看 topk 的两层重载。先是把 `EEPTPU_RESULT` 摊平成 `vector<float>`：

[main.cpp:L223-L236](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L223-L236) — 这个 `get_topk` 重载按 `shape[0..3]` 之积确定长度，把 `result.data` 逐个拷进 `cls_scores`，再委托给下面那个纯向量的重载。

然后是真正的 partial_sort：

[main.cpp:L198-L221](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L198-L221) — 构造 `(score, index)` pair，用 `std::partial_sort(..., std::greater<pair>())` 取降序前 `topk`，再把 `vec[i].first`(得分) 与 `.second`(类别 ID) 填进 `top_list`。

最后是结果打印与释放。注意打印顺序是 `[class_id] score`，即 `top_list[i].second` 在前、`.first` 在后：

[main.cpp:L173-L186](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L173-L186) — 先按输出元素数对 `topk` 做上限保护（避免类别数不足 5 时越界），再 `get_topk`、打印、`results_release`。

`results_release` 负责释放每个 result 的 `data`（库用 `malloc/calloc` 分配，所以这里用 `free` 配对）：

[main.cpp:L20-L27](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L20-L27) — 遍历 `results`，`free(results[i].data)`，再 `clear()`。注意释放的是 `data` 指针，不是 vector 本身。

#### 4.3.4 代码实践

**实践目标**：搞清「软件耗时」与「硬件耗时」的差异，以及为何 topk 必须带 index。

**操作步骤**：

1. 在 main 里找到 `printf("EEPTPU forward ok, cost time(hw+sw): ...")` 与 `printf("EEPTPU hw cost: ...")`（约 169–171 行）。
2. 思考：`hw+sw` 减去 `hw` 的差值包含哪些部分？（提示：`set_input` 的数据搬运、`forward` 的驱动调用开销、结果回读拷贝。）
3. 阅读两个 `get_topk` 重载，确认：如果只对 score 排序而不带 index，能否还知道「最高分类别是几号」？

**需要观察的现象 / 预期结果**：

- `hw cost` 一定 **小于等于** `cost time(hw+sw)`；两者差值即软件/总线开销。
- 去掉 pair 里的 index，排序后只能拿到「最高分是多少」，拿不到「对应哪个类别」——所以 `(score, index)` pair 是必须的。

> 命令运行结果：待本地验证（具体毫秒数依赖板上频率与 DDR 带宽，u8-l4 会讨论）。

#### 4.3.5 小练习与答案

**练习 1**：`result` 为什么是 `vector<EEPTPU_RESULT>` 而不是单个 `EEPTPU_RESULT`？

> **答案**：因为一个网络可能有**多个输出张量**（例如某些网络同时输出分类得分和特征图）。`forward` 把所有输出都填进 vector，`result[0]` 是第一个输出。分类网络通常只有 1 个输出，所以 demo 里固定取 `result[0]`。

**练习 2**：`std::partial_sort(vec.begin(), vec.begin()+topk, vec.end(), std::greater<>())` 中，如果把 `greater` 换成默认（`less`），top5 会变成什么？

> **答案**：会变成**得分最低的 5 个**类别。`greater` 让 partial_sort 把「更大」的元素放前面，才是 top（最高分）；默认 `less` 取的是最小值。

**练习 3**：为什么 `results_release` 用 `free()` 而不是 `delete`？

> **答案**：因为 `EEPTPU_RESULT.data` 是库内部用 C 风格的 `malloc/calloc` 分配的（与 u2-l2 里 eepimg 的分配方式一致），必须用 `free` 配对释放；用 `delete` 属于 malloc/delete 混用，行为未定义。

---

### 4.4 compile.sh/test.sh 工具链

#### 4.4.1 概念说明

写好 `main.cpp` 后，要把它编译成「能在板卡上跑」的可执行文件。难点在于：**开发主机通常是 x86，板卡是 ARM64**，两者指令集不同。所以要用**交叉编译器**——在 x86 上生成 ARM64 的机器码。

`compile.sh` 把这件事做成了「传一个平台参数就自动切换编译器」的小工具。`test.sh` 则是板上运行的最小脚本，指定用哪个 bin、测哪张图。

#### 4.4.2 核心流程

`compile.sh` 的逻辑：

```
1. 读平台参数 platform（命令行传入 或 交互式询问 "32/64/86"）
2. 按 platform 选编译器与子目录名 pf：
     "32" → CPP=arm-linux-gnueabihf-g++ , pf=arm32
     "64" → CPP=aarch64-linux-gnu-g++   , pf=aarch64
     "86" → CPP=g++                      , pf=x86
3. 列出待编译源文件：main.cpp + eep_image.cpp + cnpy.cpp + npy_load.cpp
4. cflags：包含 -I../libs/${pf}/eep/include（库头文件）
   ldflags：-L../libs/${pf}/eep/lib
   libs：-leeptpu_pub（链接运行库）
5. 拼成一条 g++ 命令并执行，输出可执行文件 demo
```

关键点：`${pf}`（arm32/aarch64/x86）既决定**编译器**，又决定**链接哪个目录下的预编译库** `libeeptpu_pub`——SDK 为每个平台都准备了一份预编译库（`sdk/Readme.md` 说支持 ARM32/AARCH64/X86）。换平台 = 同时换编译器和换库。

`test.sh` 的逻辑更简单：

```
bins="../eeptpu_bins/"                                     # bin 目录
input="./input/dog-Husky_248.jpg"                          # 测试图
sudo ./demo ${bins}eeptpu_s2_mobilenet_v1.pub.bin ${input} # 跑
```

注意三点：

1. bin 文件名 `eeptpu_s2_mobilenet_v1.pub.bin` 里的 `s2` 对应 `eeptpu_compiler` 的某种编译方案（量化/精度/线程数的组合，详见 u3-l1 的 `setting.ini`），这里只需知道「不同 bin = 不同网络或不同精度」。
2. 用 `sudo` 是因为 SoC 模式要访问 `/dev/mem`（物理内存映射）或 PCIE 的 `/dev/xdma0_*`，这些都需要 root 权限。
3. 两个参数正好对上 `main` 的 `argv[1]`(bin) 和 `argv[2]`(image)。

#### 4.4.3 源码精读

先看平台→编译器的切换：

[compile.sh:L17-L26](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/compile.sh#L17-L26) — `32`→`arm-linux-gnueabihf-g++`、`64`→`aarch64-linux-gnu-g++`、`86`→`g++`；同时设 `pf` 子目录名。

再看编译命令的组装——`pf` 同时用在头文件路径、库路径和 rpath 上：

[compile.sh:L32-L42](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/compile.sh#L32-L42) — 源文件列表、`-I../libs/${pf}/eep/include`、`-L../libs/${pf}/eep/lib`、`-leeptpu_pub`，以及 `-Wl,-rpath,../libs/${pf}/eep/lib`（运行时库搜索路径）和 `-fopenmp`。

`-Wl,-rpath` 很重要：它把「去哪找 `libeeptpu_pub.so`」直接烙进可执行文件，这样在板上运行 `./demo` 时不用额外设 `LD_LIBRARY_PATH`，只要 `libs/aarch64/eep/lib/` 跟着 `demo` 一起拷过去即可。

最后看运行脚本：

[test.sh:L1-L3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/test.sh#L1-L3) — 指定 `../eeptpu_bins/eeptpu_s2_mobilenet_v1.pub.bin` 与测试图 `dog-Husky_248.jpg`，`sudo ./demo` 运行。

#### 4.4.4 代码实践

**实践目标**：在 x86 主机上交叉编译出 aarch64 的 `demo`。

**操作步骤**：

1. 确认已安装交叉编译器：`which aarch64-linux-gnu-g++`（Ubuntu 上来自 `gcc-aarch64-linux-gnu` 包）。
2. 确认 SDK 预编译库就位：`sdk/demo/libs/aarch64/eep/lib/` 下应有 `libeeptpu_pub.so`，`sdk/demo/libs/aarch64/eep/include/` 下应有 `eeptpu.h`（这两个由 `libeeptpu_pub` 分发，仓库内可能没有，需从 SDK 获取——**待本地验证**）。
3. 在主机执行：`cd sdk/demo/classify && bash compile.sh 64`，应生成可执行文件 `demo`。
4. 验证架构：`file demo`，应包含 `ARM aarch64` 字样。
5. 把 `demo`、`libs/aarch64/eep/lib/`、`eeptpu_bins/*.pub.bin`、`input/` 一起拷到板卡，在板上 `sudo bash test.sh`。

**需要观察的现象 / 预期结果**：

- `compile.sh 64` 打印出 `aarch64-linux-gnu-g++ -o demo ...` 并以 `Compile succ` 结束。
- `file demo` 显示 `ELF 64-bit LSB ... ARM aarch64`。
- 板上 `test.sh` 输出形如 `Result (top 5):` 后跟 5 行 `[类号] 得分`。

> 命令运行结果：待本地验证（需要 x86 主机装有交叉编译器、SDK 库，以及 ZynqMP 板卡）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile.sh` 里 `${pf}` 同时出现在编译器、`-I`、`-L` 和 `-rpath` 四处？

> **答案**：因为每个平台（arm32/aarch64/x86）都需要**配套的**编译器和**配套的**预编译库 `libeeptpu_pub`。`pf` 是「平台」这一变量的统一体现：换平台时，编译器、头文件目录、库目录、运行时库搜索路径必须**一起换**，否则会出现架构不匹配的链接错误或运行时找不到 `.so`。

**练习 2**：如果不加 `-Wl,-rpath,../libs/${pf}/eep/lib`，在板上运行 `./demo` 可能发生什么？

> **答案**：动态加载器找不到 `libeeptpu_pub.so`，报 `error while loading shared libraries: libeeptpu_pub.so: cannot open shared object file`。补救办法是手动 `export LD_LIBRARY_PATH=/path/to/libs/aarch64/eep/lib`，或者把 `.so` 拷到系统目录。加了 rpath 就能「带着相对路径跑」，更省事。

**练习 3**：`test.sh` 为什么要用 `sudo`？

> **答案**：SoC 模式下访问 `0xA0000000` 等物理地址要走 `/dev/mem`，PCIE 模式要走 `/dev/xdma0_*`，这些设备默认只有 root 能打开。所以 `./demo` 必须以 root 运行，否则会在 `set_interface` / 打开设备一步失败。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿任务**（对应规格里的实践要求）：

**任务**：对照 `main.cpp`，画出从 `main` 入口到打印 top5 的**完整函数调用序列**；并解释 `compile.sh` 如何在 arm32/aarch64/x86 三种平台间切换编译器。

**第一部分：调用序列（请在草稿上补全箭头右边的「做了什么」）**

```
main(argc, argv)
  → eeptpu_init(SOC, path_bin)            # ① 初始化（4.1）
       → tpu->init()                      #   工厂拿到对象
       → eeptpu_set_tpu_reg_zones(...)    #   注册寄存器 zone (0xA0000000)
       → eeptpu_set_base_address(...)     #   设数据基地址 (0x40000000)
       → eeptpu_set_interface(SOC)        #   生效
       → eeptpu_load_bin(path_bin)        #   加载网络 → 填好 input_shape
  → eeptpu_write_input(path_image)        # ② 写输入（4.2）
       → eepimg_load_image(..., BGR)      #   按通道数选像素序读图
       → eepimg_resize(orig, W, H)        #   缩放到 input_shape[3],[2]
       → eeptpu_set_input(data,c,h,w,0)   #   送入 TPU
  → eeptpu_forward(result)                # ③ 推理（4.3）
  → eeptpu_get_tpu_forward_time()         #   取硬件耗时
  → get_topk(result[0], 5, top_list)      #   取 top5
       → (重载) 把 EEPTPU_RESULT 摊平成 vector<float>
       → std::partial_sort(..., greater)  #   降序取前 5
  → printf top_list                       #   打印 [类号] 得分
  → results_release(result)               #   释放 data
  → tpu->eeptpu_close(); delete(tpu)      #   收尾
```

逐行对照 [main.cpp:L124-L194](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L124-L194) 核对你的序列是否完整，特别注意 `input_shape` 在 `load_bin` 之后才可用（所以 `write_input` 必须在 `eeptpu_init` 之后调用）。

**第二部分：编译器切换说明**

用一段话讲清三件事（答案要点已在本讲 4.4 给出，请用自己的话复述）：

1. `compile.sh` 接收平台参数 `32/64/86`，分别映射到 `arm-linux-gnueabihf-g++` / `aarch64-linux-gnu-g++` / `g++`。
2. 同时设 `pf=arm32/aarch64/x86`，用它定位 `../libs/${pf}/eep/{include,lib}` 下**配套的**预编译 `libeeptpu_pub`。
3. 因此「换平台」=「编译器 + 头文件目录 + 库目录 + rpath」一起换；在 x86 主机上 `bash compile.sh 64` 即可产出能在 ZynqMP（ARM64）上运行的 `demo`。

> 命令运行结果：待本地验证（需要交叉编译环境与板卡）。

## 6. 本讲小结

- `classify` demo 的主线是 **`eeptpu_init → eeptpu_write_input(set_input) → eeptpu_forward → get_topk → results_release`**，整个进程只 init 一次。
- 初始化要配齐三件事：**接口类型**（SoC 内存映射 / PCIE-XDMA）、**寄存器 zone**（`{core_id, addr, size}`，SoC 下 `0xA0000000`）、**数据基地址**（`set_base_address`，SoC arm64 下 `0x40000000`）——这些地址与 u1-l4 的硬件设计逐位对应。
- `input_shape` 是 **NCHW** 四元组，`[1]` 决定像素序、`[2]/[3]` 决定缩放的 H/W；`resize` 传参顺序是 `(W, H)` 即 `([3], [2])`。
- `forward` 返回 `vector<EEPTPU_RESULT>`，每个含 `data`(float 指针，需 `free`) 与 `shape[4]`；分类输出常是 `[1,C,1,1]`。
- topk 用 `(score, index)` pair + `partial_sort(greater)` 取降序前 k，复杂度 \(O(n \log k)\)；不带 index 就无法还原类别 ID。
- `compile.sh` 用 `32/64/86` 一键切换交叉编译器与配套的预编译库（`pf` 同时驱动 `-I/-L/-rpath`），在 x86 主机产出 ARM64 `demo`；`test.sh` 用 `sudo ./demo` 在板上跑 mobilenet bin。

## 7. 下一步学习建议

本讲跑通了 Linux 路线的分类 demo，接下来可以按两个方向深入：

- **横向（接口与多网络）**：下一讲 **u2-l4** 会专门对比 SoC 与 PCIE 两种接口的完整配置差异，并解释 arm32/arm64 地址不同的原因；之后 **u7-l1（multi_bins_test）** 讲多实例多核、**u7-l2（nntpu_test）** 讲多输入与 npy/pack 模式。
- **纵向（编译链路与裸机）**：想搞清 `*.pub.bin` 怎么来，进入 **U3**（`eeptpu_compiler` 的 `setting.ini` 与 `eepBinCvt`）；想看「不经过运行库、直接读写寄存器」的裸机路线，进入 **U4**（`EEPTPU_SA` 与寄存器协议）。
- **后处理对照**：本讲的 `get_topk` 是最简单的后处理；**u6-l1** 会把 Linux demo 与 standalone 的 topk 实现做对照，进一步讲清 `EEPTPU_RESULT` 到得分向量的转换。

建议先读 `sdk/demo/yolo/main.cpp` 与 `sdk/demo/icnet/main.cpp`，对照本讲的 classify，体会「初始化完全一样、只换 bin 与后处理」的解耦设计——这能帮你建立对整个 demo 集的整体感。
