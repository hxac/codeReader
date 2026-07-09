# nntpu_test：多输入、npy 数据与 pack 模式

## 1. 本讲目标

本讲是「进阶 demo」单元的第二篇。上一篇 `multi_bins_test` 解决了「一个进程跑多个网络」，本讲解决另一个进阶问题：「**如何测试一个形态更复杂的网络**」——它可能有多个输入、输入是 numpy 保存的 `.npy` 张量、使用非 FP32 的精度、或者启用了 TPU 特有的 pack 打包模式。

学完本讲，你应当能够：

- 理解 `nntpu_test` 用一个二维 `input_list` 统一描述「多输入网络 × 多次测试」的数据组织规则；
- 掌握 `.npy` 文件如何被加载（`read_npy` / `cnpy`），以及 demo 如何自动为 npy 数据补做 mean/norm；
- 认识 `data_type`（FP32/FP16/INT8/INT16/INT32/UINT8）如何随 `eeptpu_set_input` 的 `mode` 参数传入；
- 理解 v2.0 引入的 pack 输出模式，以及为何配合新编译器（v2.4.1+）后就「不再需要手动设置 pack_shape」。

本讲依赖 [u2-l3](#)（EEPTPU 高层 API 与 classify demo）与 [u5-l2](#)（输出读取），并承接 [u7-l1](#)（多实例）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，为什么需要一个「通用测试 demo」。** 前面几篇的 `classify`/`yolo`/`icnet` 都是为「特定任务 + 图像输入 + 固定后处理」量身写的。但 TPU 能跑的远不止这三类：有些网络有多个输入（例如双流网络、图文多模态），有些网络在 PC 上用 PyTorch 跑出来的中间张量需要直接灌进 TPU 对拍精度，有些网络编译成了 INT8 或 FP16。`nntpu_test` 就是一个「不假设网络长什么样」的通用探针：给它一个 bin 和一份输入，它就把输入喂进去、跑 forward、把输出原样落盘成 txt，供你比对。

**第二，什么是 `.npy`。** `.npy` 是 Python numpy 库的二进制数组存盘格式。一个 `.npy` 文件 = 一段头部（描述元素类型、形状、字节序）+ 一段连续的原始数据。它比图像更「干净」：已经是 CHW 排列的数值数组，省去了图像解码。C++ 侧读 npy 通常用一个叫 `cnpy` 的开源小库（本仓库就内置了一份），`nntpu_test` 在它之上包了一层 `read_npy`。

**第三，什么是 pack 模式。** 回顾 [u4-l4](#) 讲过的硬件输入格式：TPU 以「16 通道 × 2 字节 = 32 字节」为最小访存单元，真实通道外的字节补零。这种把数据按硬件友好的方式提前打包好、整块塞给 TPU 的输入方式就叫 **pack 模式**（与之相对的是「喂原始数据，让运行库自动打包」）。bin 文件里会记录该网络是否 pack、以及 pack 后的形状（`pack_out_c/h/w`）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `sdk/demo/nntpu_test/main.cpp` | demo 主体：参数解析、TPU 初始化、输入二维列表组织、npy/图像加载、mean/norm、set_input、forward、结果落盘 |
| `sdk/demo/common/npy/npy_load.h` | `read_npy` 的对外声明，一行函数签名 |
| `sdk/demo/common/npy/npy_load.cpp` | `read_npy` 实现：调用 `cnpy::npy_load`，解析形状与元素类型，按类型 malloc 并拷贝数据，返回 `dtype` 编码 |
| `sdk/demo/common/npy/cnpy.cpp` / `cnpy.h` | 第三方 npy 解析库：解析 npy 头部（magic、shape、descr）、把数据读进 `NpyArray` |
| `sdk/demo/nntpu_test/compile.sh` / `test.sh` | 交叉编译脚本与上板运行脚本 |

> 说明：demo 使用的 `EEPTPU` 类、`NET_INPUT_INFO`、`EEPTPU_RESULT`、`EEPTPU_REG_ZONE` 等定义来自闭源运行库 `libeeptpu_pub` 的头文件 `eeptpu.h`（打包在 `sdk/libeeptpu_pub/libeeptpu_pub_v0.7.1.tar.gz` 内，不在仓库源码树中直接可见）。下文对这些结构体的字段说明，依据的是 `main.cpp` 中的**实际用法**，而非直接读到的定义。

## 4. 核心概念与源码讲解

### 4.1 多输入 input_list 组织

#### 4.1.1 概念说明

一个网络可能有不止一个输入。比如一个双流网络要同时吃「RGB 图」和「光流图」，一个多模态网络要同时吃「图像」和「文本向量」。对这类网络，**一次 forward 需要同时喂入多个张量**，每个张量有自己的 `input_id`。

`nntpu_test` 用一个二维数组来统一描述「多输入」与「多次测试」两个维度：

```
input_list[ 组号 ][ 输入号 ]
```

- **外层（组）**：每一组 = 一次完整的 forward（一次性喂齐该网络的所有输入）。
- **内层（输入）**：这一组里要喂给网络的各个输入张量，每个带一个 `input_id`。

源码顶部用注释把这套模型讲得很清楚，并区分了「单输入网络测 N 次」与「3 输入网络测 N 次」两种情形。

#### 4.1.2 核心流程

```
命令行 --input 参数
        │
        ▼
  input_parpare()            # 解析字符串/路径，构建 input_list
        │
        ▼
  input_list[group][input]   # 二维结构
        │
        ▼
  for idx in 0..group数:
      eeptpu_write_inputs(input_list[idx], inputs_info)  # 写入本组所有输入
      tpu->eeptpu_forward(result)                         # 跑一次
      落盘结果 txt
```

关键点：`inputs_info` 由 `tpu->eeptpu_get_input_info()` 从 bin 里读出，里面记录了网络每个输入的 `input_id`、形状（c/h/w）、名字、以及 pack 信息。`eeptpu_write_inputs` 的职责就是**按 `input_id` 把本组的每个文件匹配到正确的网络输入**再写入。

#### 4.1.3 源码精读

先看描述单个输入的最小结构体，以及二维列表的声明与模型注释：

[文件路径:55-67](sdk/demo/nntpu_test/main.cpp#L55-L67) 定义了 `st_input{id, path}`，并声明了二维容器 `input_list`，上方注释正是「组×输入」二维模型的文字说明（单输入测 N 次、3 输入测 N 次）。

`eeptpu_write_inputs` 完成「按 id 匹配再写入」：

[文件路径:412-431](sdk/demo/nntpu_test/main.cpp#L412-L431) 遍历本组的每个 `st_input`，在内层循环里用 `input_id` 在 `inputs_info` 中找到对应的 `NET_INPUT_INFO`，再交给 `eeptpu_write_input` 真正加载。

主循环遍历每一组、跑 forward、落盘：

[文件路径:206-262](sdk/demo/nntpu_test/main.cpp#L206-L262) 是 demo 的推理主循环；其中 [文件路径:204](sdk/demo/nntpu_test/main.cpp#L204) 打印 `input_list[0].size()`（网络输入数）与 `input_list.size()`（测试组数）。

构建 `input_list` 的 `input_parpare` 有三条分支，靠「字符串里是否同时含 `#` 和 `;`」判断是不是多输入：

- **单文件**：[文件路径:721-728](sdk/demo/nntpu_test/main.cpp#L721-L728)，构造 1 组、组内 1 个输入（id=0）。
- **目录**：[文件路径:685-719](sdk/demo/nntpu_test/main.cpp#L685-L719)，用 `scandir` 扫描目录下所有 `jpg/png/bmp/npy/...` 文件，**全部塞进同一个组**（每个 id=0）。
- **多输入字符串**：[文件路径:732-762](sdk/demo/nntpu_test/main.cpp#L732-L762)，按 `#` 切分得到「每一段 = 一个输入」，每段再按 `;` 切成 `id` 和 `path`，最终压成**一组多输入**。

多输入字符串的格式注释见 [文件路径:669-675](sdk/demo/nntpu_test/main.cpp#L669-L675)：

> 单输入：直接是路径；
> 多输入：`id1;path1#id2;path2#...`

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：依据顶部注释，亲手画出「网络输入数=3、测试 N 次」时 `input_list` 的二维结构，并核对它与代码实际行为的关系。

**操作步骤**：

1. 打开 [文件路径:61-67](sdk/demo/nntpu_test/main.cpp#L61-L67)，按注释画出二维表：外层 N 个组，每组内层 3 个输入（id 0/1/2）。
2. 再读 `input_parpare` 的多输入分支 [文件路径:732-762](sdk/demo/nntpu_test/main.cpp#L732-L762)，确认：**一次命令行的多输入字符串只会构建出 1 个组**（只有一处 `in_list.push_back`，在 [文件路径:759](sdk/demo/nntpu_test/main.cpp#L759)）。

**需要观察的现象 / 易错点**：

- 注释里的「N 组」是**概念模型**；要从命令行构造出 N 个组，需要多次运行程序（每次换 `--input` 字符串），单次运行最多 1 组。
- 目录分支 [文件路径:685-719](sdk/demo/nntpu_test/main.cpp#L685-L719) 把目录下**所有**文件压进**同一个组**（且都是 id=0）。对一个「单输入网络」传一个含 N 张图的目录，会得到「1 组、组内 N 个输入全 id=0」的结构，这与注释的「N 组、每组 1 输入」并不一致——`eeptpu_write_inputs` 会把它们都写到 id=0，最终有效的是最后一个。**测试多张图应一张张传或多次运行**，不要直接丢一个目录给单输入网络。

**预期结果**：能口述「组 = 一次 forward 的全部输入；多输入网络的多个输入用 `id;path#id;path` 在一组内表达」。

#### 4.1.5 小练习与答案

**练习 1**：一个双输入网络（id 0 和 id 1），现在只有一组测试数据 `a.npy`(给 id 0) 和 `b.npy`(给 id 1)，请写出 `--input` 参数。
**答案**：`--input "0;a.npy#1;b.npy"`（外层 `#` 分隔两个输入，内层 `;` 分隔 id 与路径）。

**练习 2**：为什么 `eeptpu_write_inputs` 里要先在 `inputs_info` 里按 `input_id` 找一圈，而不是直接按下标 `inputs[i]` 对应 `inputs_info[i]`？
**答案**：因为命令行里用户给出的输入顺序不一定与 bin 内部记录的 `input_id` 顺序一致；用 `input_id` 匹配才能保证「用户指定的文件」喂到「网络真正期望的那个输入槽位」上，避免错位。

---

### 4.2 npy 加载与 mean/norm

#### 4.2.1 概念说明

`.npy` 让我们绕开图像解码，直接把 PyTorch/NumPy 里算好的张量喂给 TPU——这对**精度对拍**特别有用：在 PC 上用框架跑一遍拿到某层输入，存成 npy，再原样灌进 TPU，比较二者输出是否一致。

但这里有个坑：**网络的预处理（mean/norm）在编译时已经被「烤进 bin」了**（回顾 [u3-l1](#)）。也就是说，如果你给 TPU 的图像是原始像素，TPU 内部会自动减均值除标准差；可如果你给的是 npy，这个 npy 到底有没有做过 mean/norm？demo 无法假设，于是它选择**主动检测**：若发现该网络配了非默认的 mean/norm，就**先在 CPU 上把 mean/norm 减掉**，再喂给 TPU，避免被减两次或一次都不减。

#### 4.2.2 核心流程

```
read_npy(path) → src_data, shape[c,h,w], dtype
        │
        ▼
tpu->eeptpu_get_mean_norm(net_mean, net_norm)
        │
   是否全为默认(mean=0, norm=1)?
        ├── 是 → 直接用 src_data
        └── 否 → process_mean_norm：y=(x-mean)*norm，输出 float32
        │
        ▼
tpu->eeptpu_set_input(...)
```

mean/norm 是**逐通道**的：第 k 个通道的每个像素都套同一组 `mean[k]`、`norm[k]`，公式为：

\[
y_{k,j} = (x_{k,j} - \text{mean}_k)\cdot \text{norm}_k
\]

#### 4.2.3 源码精读

npy 的对外接口只有一个函数：

[文件路径:6](sdk/demo/common/npy/npy_load.h#L6) 声明 `read_npy(filepath, buff, shape1, shape2, shape3, dtype)`，其中 `shape1/2/3` 对应 c/h/w，`dtype` 返回元素类型编码。

`read_npy` 内部先调用 `cnpy::npy_load` 读出 `NpyArray`，再按元素类型分支处理。形状解析会兼容 1~4 维（4 维取 NCHW、3 维取 CHW、2 维取 HW、1 维取长度），见 [文件路径:53-84](sdk/demo/common/npy/npy_load.cpp#L53-L84)。元素类型分发见 [文件路径:86-184](sdk/demo/common/npy/npy_load.cpp#L86-L184)：`f4→FP32`、`f2→FP16`、`i4→INT32`、`i2→INT16`、`u1→UINT8`、`i1→INT8`，其余报错。`cnpy` 侧真正解析 npy 头部（读 magic、解析 `descr` 得到字节序与元素类型、解析 `shape`、解析 `fortran_order`）在 [文件路径:133-190](sdk/demo/common/npy/cnpy.cpp#L133-L190)。

回到 `main.cpp`，npy 输入的加载与 mean/norm 检测集中在 `eeptpu_write_input` 的 npy 分支：

[文件路径:441-539](sdk/demo/nntpu_test/main.cpp#L441-L539)。其中：

- [文件路径:447](sdk/demo/nntpu_test/main.cpp#L447) 调 `read_npy` 拿到数据、形状、`dtype`。
- [文件路径:472-480](sdk/demo/nntpu_test/main.cpp#L472-L480) 用 `eeptpu_get_mean_norm` 取网络的 mean/norm，用 `is_vector_all_value` 判是否全为默认值（mean 全 0、norm 全 1）；若非默认，打印警告，提示「请确认你的 npy 是否已做过 mean/norm」。
- [文件路径:481-513](sdk/demo/nntpu_test/main.cpp#L481-L513) 实际执行 CPU 侧 mean/norm：先按通道数补齐 `use_mean/use_norm`（兼容「删尾通道」模式，见 [文件路径:486-494](sdk/demo/nntpu_test/main.cpp#L486-L494)），再按 `dtype` 选模板调 `process_mean_norm`，最后把 `data_type` 强制改回 `1`（float32）。
- [文件路径:395-409](sdk/demo/nntpu_test/main.cpp#L395-L409) 就是 `process_mean_norm` 模板：双层循环，外层通道、内层像素，`*pd++ = (*ps++ - mean[k]) * norm[k]`，正是上面的公式。

#### 4.2.4 代码实践（源码阅读型 + 本地可选）

**实践目标**：理解「npy + 网络配了 mean/norm」时 demo 的双保险逻辑。

**操作步骤**：

1. 读 [文件路径:472-475](sdk/demo/nntpu_test/main.cpp#L472-L475) 的判据：`is_vector_all_value<float>(net_mean, 0.0) == false || is_vector_all_value<float>(net_norm, 1.0) == false`。即「只要 mean 不全为 0 或 norm 不全为 1」就触发 CPU 预处理。
2. 跟踪 `src_data` 与 `in_data` 两个指针：[文件路径:469-470](sdk/demo/nntpu_test/main.cpp#L469-L470) 默认 `in_data = src_data`、`b_in_data_need_free = false`；一旦做了 mean/norm，[文件路径:496-497](sdk/demo/nntpu_test/main.cpp#L496-L497) 新 malloc 一段 float 缓冲并置 `b_in_data_need_free = true`。
3. （本地可选）用 Python 生成一个 npy：`import numpy as np; np.save("x.npy", np.random.rand(3,224,224).astype(np.float32))`，再在板子上跑 `./demo --bin xx.pub.bin --input x.npy`，观察串口是否打印 `Warning: Input is npy file, and found 'mean,norm'`（取决于该 bin 是否配了 mean/norm）。

**预期结果**：能解释「为什么 demo 要主动替 npy 做 mean/norm」——因为 mean/norm 已烤进 bin，TPU 内部会再做一次，npy 必须提前减掉以避免重复或遗漏。第 3 步若无硬件，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果网络的 mean/norm 全是默认值（mean=0、norm=1），npy 数据会被做 mean/norm 吗？
**答案**：不会。[文件路径:475](sdk/demo/nntpu_test/main.cpp#L475) 的 `if` 条件为假，直接走 `in_data = src_data`，原样喂入；此时要求 npy 本身已经是网络期望的输入分布。

**练习 2**：为什么做完 `process_mean_norm` 后要把 `data_type` 改成 `1`？
**答案**：因为 `process_mean_norm` 的输出缓冲是 `float*`（[文件路径:496](sdk/demo/nntpu_test/main.cpp#L496)），无论输入是 FP16/INT8 还是别的，处理后都变成了 float32，所以要把传给 `eeptpu_set_input` 的类型标识同步改成 FP32(=1)。

---

### 4.3 data_type 与精度

#### 4.3.1 概念说明

TPU 支持多种数据精度：FP32、FP16、INT8、INT16、INT32、UINT8（回顾 [u1-l1](#)，免费版实际开放 FP16 与 INT8）。当输入是 npy 时，这份 npy 的元素类型可能各不相同——`read_npy` 会从 npy 头部的 `descr` 字段解析出来，用一个整数 `data_type` 编码返回。

demo 把「数据怎么喂」抽象成了 `eeptpu_set_input` 的 **mode 参数**：

- `mode=0`：图像输入（传图像像素，mean/norm 与像素→张量的转换交给运行库）。
- `mode=1`：npy / 浮点输入，**需要额外传 `data_type`** 告诉运行库元素格式，以便它转成 TPU 内部格式。
- `mode=2`：raw 原始数据（数据已是硬件期望的打包格式，运行库原样搬运，不做转换——见 4.4 的 pack 模式）。

#### 4.3.2 核心流程

```
read_npy → dtype ∈ {1:FP32, 2:FP16, 3:INT8, 4:INT16, 5:INT32, 7:UINT8}
        │
        ▼
按 dtype 选模板（input_save_txt / process_mean_norm 都是模板）
        │
        ▼
eeptpu_set_input(id, data, c, h, w, mode=1, data_type)   # 非 pack
   或
eeptpu_set_input(id, data, c, h, w, mode=2)              # pack：raw，不传 data_type
```

`data_type` 编码定义在 npy 这一侧：

[文件路径:12-21](sdk/demo/common/npy/npy_load.cpp#L12-L21) 给出编码常量 `FP32=1, FP16=2, INT8=3, INT16=4, INT32=5, UINT8=7`，并注释「TPU 不支持 uint16/uint32」。

#### 4.3.3 源码精读

`eeptpu_write_input` 的 npy 分支里有两处按 `data_type` 的 `switch`，分别用于「落盘调试」和「mean/norm 处理」：

- 落盘：[文件路径:455-467](sdk/demo/nntpu_test/main.cpp#L455-L467)，按 `dtype` 用对应类型指针把输入写成 txt，方便人工核对。
- mean/norm：[文件路径:498-507](sdk/demo/nntpu_test/main.cpp#L498-L507)，同样按 `dtype` 选模板实例化 `process_mean_norm<T>`。

真正把精度告诉 TPU 的，是 `set_input` 的分支：

- 非 pack（`pack_out_c == 0`）：[文件路径:515-522](sdk/demo/nntpu_test/main.cpp#L515-L522)，`eeptpu_set_input(id, src_data, c, h, w, 1, data_type)`——**mode=1，并带上 data_type**。
- pack（`pack_out_c > 0`）：[文件路径:523-529](sdk/demo/nntpu_test/main.cpp#L523-L529)，`eeptpu_set_input(id, src_data, c, h, w, 2)`——**mode=2，不传 data_type**，因为数据已是 raw 打包格式。

图像分支同理：非 pack 用 `mode=0`（[文件路径:574-590](sdk/demo/nntpu_test/main.cpp#L574-L590)），pack 用 `mode=2`（[文件路径:592-595](sdk/demo/nntpu_test/main.cpp#L592-L595)）。

> 旁注：`eeptpu_set_input` 在运行库里存在**多个重载**（带 `input_id` 的多输入版本、不带 `input_id` 的旧式单输入版本，[文件路径:587](sdk/demo/nntpu_test/main.cpp#L587) 就是一处不带 id 的旧式调用）。本讲以「带 id + mode」的主路径为准。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：搞清「同一份 npy，在 pack 与非 pack 两种网络下，喂法为何不同」。

**操作步骤**：

1. 对比 [文件路径:515-522](sdk/demo/nntpu_test/main.cpp#L515-L522) 与 [文件路径:523-529](sdk/demo/nntpu_test/main.cpp#L523-L529) 两段 `set_input`，记录二者在 `mode` 与「是否传 data_type」上的差异。
2. 解释：非 pack 时为何要传 `data_type`，pack 时为何不传。

**需要观察的现象**：非 pack 走 mode=1，运行库需要 `data_type` 才能把 FP16/INT8 等正确转成 TPU 内部格式；pack 走 mode=2，数据已经是硬件 raw 格式，运行库只负责搬运，不需要类型信息。

**预期结果**：能口述「mode 编码了『数据是否需要运行库转换』；data_type 只在需要转换时才有意义」。

#### 4.3.5 小练习与答案

**练习 1**：一份 `float16` 的 npy（`descr='<f2'`），`read_npy` 返回的 `dtype` 是多少？
**答案**：`2`（FP16）。对应 [文件路径:103-118](sdk/demo/common/npy/npy_load.cpp#L103-L118) 的 `f2` 分支与 [文件路径:13](sdk/demo/common/npy/npy_load.cpp#L13) 的 `DAT_FORMAT_FP16=2`。

**练习 2**：若网络是 pack 模式，npy 的 `data_type` 还会被传给 `eeptpu_set_input` 吗？为什么？
**答案**：不会。pack 模式走 [文件路径:525-528](sdk/demo/nntpu_test/main.cpp#L525-L528) 的 mode=2 分支，不传 `data_type`；因为 pack 数据已是硬件期望的 raw 布局，运行库原样搬运、不做格式转换，类型信息无用武之地。

---

### 4.4 pack 输出模式

#### 4.4.1 概念说明

pack 模式让用户**直接提供硬件打包格式的输入/输出**，跳过运行库的打包/解包步骤。它常用于两种场景：一是追求极致延迟、省掉运行库内部的格式转换开销；二是输入本身已经是别的 TPU 工具产生好的 raw 张量。

一个网络是否 pack、pack 后的形状是多少，记录在 bin 里，运行库通过 `eeptpu_get_input_info()` 读出来，填进 `NET_INPUT_INFO` 的 `pack_type` 与 `pack_out_c/h/w` 字段。**若 `pack_out_c==0`，说明该网络不是 pack 模式。**

本 demo 的一个重要演进是 v2.0 引入的「自动 pack shape」：

[文件路径:20-29](sdk/demo/nntpu_test/main.cpp#L20-L29) 的版本注释里写明：

> v2.0：支持 libeeptpu_pub.so v0.7.0（支持多输入）；**若使用 pack 模式，无需再设 pack_shape（前提是 bin 由 compiler-v2.4.1 或更新版本生成）**。

也就是说：**新编译器（v2.4.1+）会把 pack shape 写进 bin**，运行库 `eeptpu_get_input_info` 能直接读出来；只有老版本 bin（pack shape 没存进 bin）才需要用命令行 `--pack_shape` 手动补。

#### 4.4.2 核心流程

```
tpu->eeptpu_get_input_info(inputs_info)        # 读出每个输入的 pack_out_c/h/w
        │
   inputs_info[0].pack_out_c/h/w 是否为 0?
        ├── 全为 0（老 bin 且未带 pack shape）
        │       └── 用户是否传了 --pack_shape？
        │              ├── 是 → 用用户值覆盖（[文件路径:183-189](sdk/demo/nntpu_test/main.cpp#L183-L189)）
        │              └── 否 → 保持 0（非 pack）
        └── 非全 0（bin 自带 pack shape，新编译器）
                └── 即使用户传了 --pack_shape 也忽略，并提示（[文件路径:191-197](sdk/demo/nntpu_test/main.cpp#L191-L197)）
        │
        ▼
加载输入时：pack_out_c>0 → 按 pack 分辨率读图、set_input mode=2(raw)
```

#### 4.4.3 源码精读

先看 demo 打印每个输入的 pack 信息：

[文件路径:165-176](sdk/demo/nntpu_test/main.cpp#L165-L176) 遍历 `inputs_info`，打印每个输入的 `input_id`、`c/h/w`、`name`；若 `pack_type>0` 还会打印 `pack_type` 与 `pack_out_c/h/w`（[文件路径:171-174](sdk/demo/nntpu_test/main.cpp#L171-L174)）。

接着是 pack shape 的「自动 vs 手动」裁决逻辑：

[文件路径:178-197](sdk/demo/nntpu_test/main.cpp#L178-L197)。判据是 `inputs_info[0].pack_out_c/h/w` 是否为 0：

- 全为 0：若用户传了 `--pack_shape`（三个值都非 0），则用用户值覆盖（[文件路径:183-189](sdk/demo/nntpu_test/main.cpp#L183-L189)）——这是给「老 bin」的兜底。
- 非全 0：说明 bin 自带 pack shape；即使用户传了 `--pack_shape`，也忽略并提示「我们用 bin 文件里的 pack shape」（[文件路径:191-197](sdk/demo/nntpu_test/main.cpp#L191-L197)）。

`--pack_shape` 的命令行解析：

[文件路径:300-301](sdk/demo/nntpu_test/main.cpp#L300-L301) 的 usage 说明 `--pack_shape` 格式为 `c,h,w`，且**只对早于 v2.4.1 编译器生成的 bin 有效**；[文件路径:326-330](sdk/demo/nntpu_test/main.cpp#L326-L330) 用 `sscanf("%d,%d,%d")` 把它解析进 `appargs.pack_output_c/h/w`。

最后看 pack 在「加载输入」时的两个落点：

- npy 分支：[文件路径:515-529](sdk/demo/nntpu_test/main.cpp#L515-L529)，`pack_out_c==0` 走 mode=1（带 data_type），否则走 mode=2（raw）。
- 图像分支：[文件路径:547-552](sdk/demo/nntpu_test/main.cpp#L547-L552)，pack 时把 `use_c/h/w` 设为 `pack_out_c/h/w`，于是 [文件路径:554-567](sdk/demo/nntpu_test/main.cpp#L554-L567) 会按 pack 分辨率加载图像，再在 [文件路径:592-595](sdk/demo/nntpu_test/main.cpp#L592-L595) 以 mode=2 喂入。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：回答本讲标题里的核心问题——「pack 模式下为何不再需要手动设置 pack_shape」。

**操作步骤**：

1. 读版本注释 [文件路径:26-27](sdk/demo/nntpu_test/main.cpp#L26-L27)，记下「compiler v2.4.1+ 会把 pack shape 存进 bin」这一前提。
2. 读 [文件路径:165-176](sdk/demo/nntpu_test/main.cpp#L165-L176)，确认 `eeptpu_get_input_info` 能把 bin 里的 pack shape 读进 `pack_out_c/h/w`。
3. 读 [文件路径:191-197](sdk/demo/nntpu_test/main.cpp#L191-L197)，确认「bin 自带 pack shape 时，即使用户传 `--pack_shape` 也会被忽略」。

**需要观察的现象 / 预期结果**：当 bin 由新编译器生成时，`inputs_info[0].pack_out_c/h/w` 非零，demo 自动采用它；`--pack_shape` 形同虚设。**结论**：因为 pack shape 已被编译器写进 bin、又被运行库自动读出，整条链路自描述，用户无需再手动指定。`--pack_shape` 只是为兼容老 bin（pack shape 缺失）保留的兜底入口。

#### 4.4.5 小练习与答案

**练习 1**：demo 如何判断一个网络「不是 pack 模式」？
**答案**：看 `inputs_info[i].pack_out_c`（以及 h、w）是否为 0。全为 0 即非 pack，`set_input` 走 mode=0/1；任一非 0 即 pack，走 mode=2（[文件路径:515-529](sdk/demo/nntpu_test/main.cpp#L515-L529)）。

**练习 2**：一个用老编译器（v2.4.1 之前）生成的 pack 网络，bin 里没存 pack shape，用户忘了传 `--pack_shape`，会发生什么？
**答案**：`inputs_info[0].pack_out_c/h/w` 全为 0，且 `appargs.pack_output_c/h/w` 也为 0，[文件路径:181-189](sdk/demo/nntpu_test/main.cpp#L181-L189) 的两个 `if` 都不成立，pack shape 保持 0。随后在 `eeptpu_write_input` 里 `pack_out_c==0` 被当成「非 pack」走 mode=1，与该网络真实需要的 raw pack 喂法不符，会导致输入格式错误、推理结果异常。所以老 bin + pack 必须显式传 `--pack_shape`。

---

## 5. 综合实践

**任务**：用本讲四个模块的知识，完整跟踪一次「3 输入网络、FP16 npy 输入、pack 模式」的调用链，并标注每一处关键决策点。

**步骤**：

1. **构造命令行**。仿照 [文件路径:669-675](sdk/demo/nntpu_test/main.cpp#L669-L675) 的格式，写出三个 npy 输入的 `--input` 参数（答案：`--input "0;a.npy#1;b.npy#2;c.npy"`）。
2. **跟踪 input_parpare**。在 [文件路径:732-762](sdk/demo/nntpu_test/main.cpp#L732-L762) 确认：由于字符串含 `#` 和 `;`，`b_multi_input=true`，按 `#` 切 3 段、每段按 `;` 切出 id 与 path，最终 `input_list` = 1 组、组内 3 个输入。
3. **跟踪 npy 加载**。对每个 npy，[文件路径:447](sdk/demo/nntpu_test/main.cpp#L447) 的 `read_npy` 返回 `dtype=2`（FP16）。
4. **跟踪 mean/norm**。若该网络配了 mean/norm，[文件路径:498-501](sdk/demo/nntpu_test/main.cpp#L498-L501) 会用 `process_mean_norm<short>` 处理，并把 `data_type` 改成 1。
5. **跟踪 pack 喂入**。因为是 pack 网络，`pack_out_c>0`，走 [文件路径:523-529](sdk/demo/nntpu_test/main.cpp#L523-L529) 的 mode=2，raw 喂入、不传 data_type。
6. **跟踪 forward 与落盘**。[文件路径:211-262](sdk/demo/nntpu_test/main.cpp#L211-L262)：写输入 → forward → 打印耗时（[文件路径:221-223](sdk/demo/nntpu_test/main.cpp#L221-L223)，含软件墙钟 `et-st` 与硬件耗时 `eeptpu_get_tpu_forward_time`）→ 把每个输出按 `appargs.txt_col` 列写成 `test_output_data*.txt`（[文件路径:233-257](sdk/demo/nntpu_test/main.cpp#L233-L257)）。

**交付物**：一张从「命令行字符串」到「输出 txt」的完整调用链图，图上标出：多输入切分、npy 类型解析、mean/norm 检测、pack→mode=2、forward 双计时、结果落盘六个决策点各自对应的源码行号。

**待本地验证**：若手头有 ZynqMP 板卡与对应 bin，可用 `compile.sh 64` 交叉编译后在板上用 `test.sh`（[文件路径](sdk/demo/nntpu_test/test.sh)）跑通真实流程；否则标注「待本地验证」，仅完成源码层面的链路跟踪。

## 6. 本讲小结

- `nntpu_test` 是一个「不假设网络形态」的通用探针：给 bin 和输入，跑 forward，把输出原样落盘，用于精度对拍与功能验证。
- 输入用二维 `input_list[group][input]` 组织：一组 = 一次 forward 的全部输入；多输入用 `id;path#id;path` 字符串表达，`eeptpu_write_inputs` 按 `input_id` 精确匹配到网络输入槽位。
- `.npy` 经 `read_npy`（底层 `cnpy`）解析头部得到形状与元素类型；demo 会检测网络的 mean/norm，若非默认就用 `process_mean_norm` 在 CPU 上提前处理，避免与 bin 内已烤入的预处理重复。
- `data_type` 编码（FP32=1/FP16=2/INT8=3/INT16=4/INT32=5/UINT8=7）只在 `set_input` 的 mode=1（需要转换）时才有意义；mode=0 是图像、mode=2 是 raw 打包。
- pack 模式下输入按 `pack_out_c/h/w` 加载并以 mode=2 原样喂入；v2.0 配合 compiler v2.4.1+ 后，pack shape 写进 bin、由 `eeptpu_get_input_info` 自动读出，故无需再手动设 `--pack_shape`（后者仅作老 bin 的兜底）。
- demo 同时给出软件墙钟耗时与硬件纯计算耗时两个指标，便于拆分「TPU 算力」与「端到端延迟」。

## 7. 下一步学习建议

- 若想看 pack/raw 数据格式在硬件侧到底长什么样，回到 [u4-l4](#)（裸机输入预处理与 32 字节步长打包）和 [u5-l2](#)（epmat 输出读取与反量化），它们从硬件角度解释了「为什么需要 pack」。
- 若关心多网络多核的并发执行而非单网络多输入，对照 [u7-l1](#)（`multi_bins_test` 多实例与多核 reg zone）。
- 进阶可阅读 [u8-l4](#)（性能、精度与移植实践），把本讲的「data_type/精度」「pack」与编译参数（`setting.ini` 的 `--int8`、`--tpu_threads`）串成一条完整的「换网络、换精度、换板卡」移植路径。
- 继续阅读源码建议：精读 `eeptpu_write_input` 的图像分支（[文件路径:540-603](sdk/demo/nntpu_test/main.cpp#L540-L603)），对比它与 npy 分支在「谁负责 mean/norm」「谁负责打包」上的分工差异。
