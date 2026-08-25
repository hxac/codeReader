# Flash_fwd_params:贯穿 CPU 与 GPU 的参数结构体

## 1. 本讲目标

上一讲(u3-l1)我们读完了 `decode_api.cpp` 的调用链骨架,当时把 `Flash_fwd_params params;` 这一行当作链路上的一个"站点"标了出来,没有打开。本讲就打开它:这个结构体是 BitDecoding 从原版 FlashAttention 继承下来、又为低比特 KV cache 大幅扩建的**参数包**——CPU 侧的 `set_params_*` 系列函数负责往里填值,GPU 侧的 qpack/splitkv/residual/combine 四类 kernel 从里面取值,一份结构体贯穿整个调用链。学完本讲,你应该能够:

1. 逐组说出 `Qkv_params` / `Flash_fwd_params` 相比原版 FlashAttention 为低比特缓存新增的字段:`K_pack`/`k_params`/`v_pack`/`v_params` 及其 `new` 变体的指针与四级 stride、`quant_mode`、`group_size`、`new_lens` 等;
2. 解释 `set_params_fprop` 中 `K_pack_row_stride = k_pack.stride(-3)` 而 `v_params_row_stride = v_params.stride(-1)` 这种**非对称提取**的来源——字段的命名按"逻辑角色"而不是"物理维度下标";
3. 区分 `cu_seqlens_k`(指针,decode 路径下被复用为"每批次已打包主缓存长度")、`seqlen_k`(标量,打包区总长度)、`new_lens`(残余区有效长度)三个容易混淆的字段各自的语义与消费位置。

本讲不深入 `num_splits_heuristic` 的 occupancy 算法(u3-l3),也不深入 kernel 内部的主循环(u5),只把"参数怎么填、kernel 怎么用"这一层讲透。

## 2. 前置知识

**C 结构体与继承。** `struct A { ... }; struct B : public A { ... };` 表示 B 在 A 的全部成员之外再追加自己的成员。本讲的 `Flash_fwd_params` 公有继承 `Qkv_params`,一个 `Flash_fwd_params` 对象里同时躺着两代字段。CUDA kernel 参数和 C++ 函数参数一样,参数个数太多会严重影响可读性,所以常见做法是把几十个参数打包成一个"参数结构体",一次构造、处处按引用或按值传递。

**stride(步长)。** 对一个形状为 \((n_0, n_1, \dots, n_{k-1})\) 的**连续**(contiguous)张量,沿第 \(j\) 维前进一格所跨过的元素数是

\[
\text{stride}(j) \;=\; \prod_{i > j} n_i ,
\qquad \text{stride}(k-1) = 1 .
\]

例如形状 \((2, 62, 8, 128)\) 的连续张量,stride 依次是 \(62 \times 8 \times 128 = 63488\)、\(8 \times 128 = 1024\)、\(128\)、\(1\)。PyTorch 中 `t.stride()` 返回这个元组,`t.stride(-1)` 是最后一维的步长。注意:**stride 计的是元素个数,不是字节数**——这条约定在 `decode_api.cpp` 里也有原注释强调。

**`at::Tensor` 的两个底层接口。** `t.data_ptr()` 拿到这块显存在当前进程地址空间里的裸指针(类型擦除为 `void*`),`t.stride(i)` 拿到第 \(i\) 维步长。C++ 侧所有"把张量交给 kernel"的工作,本质上就是提取这两样东西,再配上形状元信息。

**kernel 参数按值传递与 `__grid_constant__`。** CUDA 允许把一个结构体**按值**作为 kernel 参数:`kernel<<<grid, block, smem, stream>>>(params)` 会把整个结构体拷贝进 kernel 的参数空间(constant bank),device 代码里读 `params.xxx` 就是读这份拷贝。sm_80 以上项目常用 `__grid_constant__` 修饰参数以获得更好的布局保证——本项目的启动宏就是这么做的(见 4.1.3)。

**CuTe 的 `make_tensor` / `make_stride`(感性认识即可)。** CUTLASS 的 CuTe 用"形状 + 每个逻辑坐标的步长"来描述一块内存,例如 `make_tensor(指针, Shape<kBlockP, kHeadDim>{}, make_stride(row_stride, _1{}))` 定义了一个二维视图:第 0 个坐标每前进 1 走 `row_stride` 个元素,第 1 个坐标每前进 1 走 1 个元素。本讲只需要能看懂这种"视图定义"——它正是结构体里那些 stride 字段的最终消费者;CuTe 本身到第五单元再深入。

**承接 u2-l1 的布局结论。** 本讲反复用到第二单元推导过的张量布局(k-channel 模式、`pack_nums = 16/num_bits`、`group_size` 分组),建议先回去扫一眼 u2-l1 的形状表。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [csrc/bit_decode/src/include/flash.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L24-L98) | **本讲主角之一**:`Qkv_params` 与 `Flash_fwd_params` 的定义 | 全文约 200 行,重点 24-98 |
| [csrc/bit_decode/decode_api.cpp](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L23-L52) | **本讲主角之二**:两个填参函数 `set_params_fprop` / `set_params_fprop_qpack` 与 `mha_fwd_kvcache` 的补填 | 23-181、440-500、534-600 |
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1312-L1353) | **消费侧**:kernel 如何用这些 stride 计算地址 | 只读 674-717、1312-1353 两段 |
| [csrc/bit_decode/src/include/block_info.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L15-L37) | **消费侧**:`cu_seqlens_k` 的读取语义 | 全文 51 行 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L83-L93) | 编译期常量:`kBlockP` / `kBlockK_params` 等 | 只读 83-93 |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L14-L39) | kernel 定义宏:`__grid_constant__` 按值传参 | 只读 14-39 |
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82) | 打包张量的**真实分配处**,形状的唯一权威 | 40-45、78-82、110-113 |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L714-L722) | 模型侧两种 quant_mode 的分配对照 | 714-722、747-750 |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21-L24) | Python 侧把 batch 折叠进 dim0 的 reshape | 21-24 |

## 4. 核心概念与源码讲解

### 4.1 Qkv_params 与 Flash_fwd_params:一份参数包,服务四个 kernel

#### 4.1.1 概念说明

`mha_fwd_kvcache` 的 Python 侧签名有 26 个参数(u3-l1 数过),C++ 侧校验完还要再传给 kernel。如果把这些张量指针、stride、形状、开关全部平铺成 kernel 的函数参数,签名会灾难性地长,而且每个 template 实例都要重复一遍。FlashAttention 的解法是造一个**参数结构体**:CPU 侧一次性填充,GPU 侧全程只读。

BitDecoding 在这个结构体上做了"两代"扩建,恰好对应继承的两层:

- **基类 `Qkv_params`:回答"数据在哪、每走一步跨多少"。** 全部是 KV/Q 数据的裸指针、四级 stride(batch/row/head/dim)、头数,以及三个量化配置字段(`quant_mode`、`group_size`、`new_lens`)。qpack 打包 kernel 只需要这一层就能工作——它读 FP16 的 K/V,写打包后的 pack/params。
- **派生类 `Flash_fwd_params`:回答"这次注意力怎么算"。** 输出指针、LSE 缓冲、维度、softmax 缩放、mask 窗口、split 数、新 KV(残余区)指针、paged-KV 表、各种 bool 开关。解码注意力 kernel 需要完整两层。

为支持低比特缓存,相比原版 FlashAttention 新增的字段集中在基类:**11 个数据指针**(原版只有 q/k/v 三个)与对应的 **batch/row/head/dim 四级 stride 家族**,外加 `quant_mode`/`group_size`/`new_lens` 三个配置。

#### 4.1.2 核心流程

以 decode 路径为例,这份结构体的"一生"是:

```text
Flash_fwd_params params;                        # 栈上分配,内容未定义
        │
        ▼  set_params_fprop (decode_api.cpp:416 调用)
清零 params = {}; 填 q/out 指针与四级 stride、
K_pack/k_params/v_pack/v_params 指针与四级 stride、
维度、softmax 缩放、窗口/因果开关
        │
        ▼  mha_fwd_kvcache 内补填 (decode_api.cpp:440-500)
残余区存在时: knew/vnew 指针与 stride、
new_lens、seqlen_knew、
k_pack_new 等 4 个 *_new 指针与四级 stride、
cu_seqlens_k ← 每批次已打包长度
        │
        ▼  set_params_splitkv (decode_api.cpp:512 调用)
num_splits、softmax_lseaccum/out_accum 缓冲指针
        │
        ▼  run_mha_fwd dispatch (decode_api.cpp:519)
读 params.quant_mode(字符串)/group_size(int) → 选模板实例
        │
        ▼  kernel<<<grid, threads, smem, stream>>>(params)
整个结构体按值拷贝进 kernel 参数空间(__grid_constant__)
        │
        ▼  device 代码读 params.xxx
residual/splitkv/combine kernel: 读指针、stride、new_lens、
num_splits、seqlen_k、cu_seqlens_k ...
```

qpack 路径则简单得多:`set_params_fprop_qpack` 一次填完(含 FP16 原始 K/V 指针),直接启动 `flash_qpack_kernel`。**同一个结构体类型服务了 qpack、splitkv、residual、combine 四类 kernel**——这就是标题里"贯穿"的含义。

#### 4.1.3 源码精读

**指针家族。** 基类开头列出了 11 个数据指针:

```cpp
// The QKV matrices.
void *__restrict__ q_ptr;
void *__restrict__ k_ptr;
void *__restrict__ K_pack_ptr;
void *__restrict__ k_pack_new_ptr;
void *__restrict__ k_params_new_ptr;
void *__restrict__ k_params_ptr;
void *__restrict__ v_ptr;
void *__restrict__ v_pack_ptr;
void *__restrict__ v_pack_new_ptr;
void *__restrict__ v_params_ptr;
void *__restrict__ v_params_new_ptr;
```

[flash.h:28-38](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L28-L38) 这段定义了三组数据:Q(`q_ptr`)、低比特主缓存(`K_pack_ptr`/`k_params_ptr`/`v_pack_ptr`/`v_params_ptr`,即第二单元讲的四个 pack/params 张量)、残余块攒满时新量化的输出(`k_pack_new_ptr` 等 4 个 `new` 变体)。`k_ptr`/`v_ptr` 是 FP16 原始 K/V——在 decode 路径它们**不被赋值**(见 4.2.3),只在 qpack 路径作为输入启用。`__restrict__` 是给编译器的"这些指针互不别名"承诺,利于向量化。

**四级 stride 家族。** 每个张量有 batch/row/head 三级,[flash.h:41-81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L41-L81) 按这个顺序成组声明;params 类张量额外多出第四级 `dim` stride:

```cpp
index_t k_params_dim_stride;
index_t k_params_new_dim_stride;

index_t v_params_dim_stride;
index_t v_params_new_dim_stride;
```

[flash.h:83-87](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L83-L87) 单独列出了这四个——为什么只有 params 需要、而且 K 系和 V 系的提取方式相反,是 4.2 的主题。`index_t` 是 `int64_t` 的别名([flash.h:25](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L25)),长序列下 batch stride 很容易超过 21 亿。

**量化配置三字段与头数:**

```cpp
// The number of heads.
int h, h_k;
int h_h_k_ratio; // precompute h / h_k,

std::string quant_mode;
int group_size;
int new_lens;
```

[flash.h:89-97](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L89-L97) 定义了 GQA 头数(`h` 个 Q 头共享 `h_k` 个 KV 头,预计算比值)和三个量化字段。注意 `quant_mode` 是 **`std::string`**——它只在 **host 侧**的 dispatch 里做字符串比较(`decode_api.cpp:199` 的 `params.quant_mode == "k-channel"`),device 代码从不读它;真正的量化配置在 dispatch 时已经变成了 kernel 的**模板参数**(u3-l1 讲过的 `run_mha_fwd_splitkv_dispatch<..., 1, num_bits, 128>`,其中 `1` 就是 k-channel)。而 `new_lens` 是 `int`,在 device 侧被真实读取(见下面的实践)。

**派生类的增量字段。** [flash.h:102-198](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L102-L198) 按功能分组追加:输出 `o_ptr`/`oaccum_ptr` 与 stride([104-111](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L104-L111));softmax 的 LSE 与 split 累积缓冲指针([116-118](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L116-L118));维度字段 `b, seqlen_q, seqlen_k, seqlen_knew, d, ...`([121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L121));softmax 缩放([123-125](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L123-L125));`cu_seqlens_q`/`cu_seqlens_k`/`seqused_k`([127-133](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L127-L133));残余区新 KV 的 `knew_ptr`/`vnew_ptr` 与三级 stride([137-147](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L137-L147));paged-KV 的 `block_table` 与两个页块大小([156-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L156-L160));dropout/窗口/软上限/随机数状态;`num_splits`([191](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L191));最后两个 bool 标记 `unpadded_lse` 和 `seqlenq_ngroups_swapped`([196-197](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L196-L197),后者就是 u3-l1 讲的 GQA 重排标记)。

**kernel 怎么接收它。** 所有 kernel 都由两个宏定义,参数一律是按值的 `const Flash_fwd_params params`:

```cpp
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
#define KERNEL_PARAM_MODIFIER __grid_constant__
#else
#define KERNEL_PARAM_MODIFIER
#endif

#define DEFINE_FLASH_QPACK_KERNEL(kernelName, ...) \
template<typename Kernel_traits> \
__global__ void kernelName(KERNEL_PARAM_MODIFIER const Flash_fwd_params params)
```

[flash_fwd_launch_template.h:14-31](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L14-L31) 说明 sm_80 以上用 `__grid_constant__` 修饰;启动处如 `kernel<<<grid, Kernel_traits::kNThreads, smem_size, stream>>>(params)`([flash_fwd_launch_template.h:194](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L194))把结构体整体拷进 kernel 参数空间。一个值得留意的工程细节:结构体里躺着 `std::string quant_mode`,携带非平凡可拷贝成员的结构体按值传 kernel 参数并非好实践(通常要求 trivially copyable)——本项目能工作,靠的是 device 代码从不触碰这个字符串;把它换成 `int` 枚举会是更稳妥的写法,这一点在 u7-l4 的架构评审里还会出现。

#### 4.1.4 代码实践

**实践目标:** 用 grep 证实"`quant_mode` 字符串只在 host 消费、`new_lens` 在 device 消费",并弄清 `new_lens` 在 kernel 里的四个用途。

**操作步骤:**

1. 在仓库根目录执行:

```bash
grep -rn "params\.quant_mode" csrc/bit_decode/src/ | grep -v flash_api.h
grep -rn "params\.new_lens"   csrc/bit_decode/src/flash_fwd_kernel.h
```

2. 逐条打开命中的行,对照下文"需要观察的现象"核对。

**需要观察的现象:** 第一条命令的命中应全部落在 `decode_api.cpp` 的 dispatch 区(199、221 两行的 `== "k-channel"` 字符串比较);第二条命令在 `flash_fwd_kernel.h` 命中 8 处,其中 2 处是 `#if DEBUG` 里的 printf(172、662),其余 6 处是真实消费。

**预期结果:** `params.new_lens` 在 device 代码中的角色(均已核对源码):

| 位置 | 用途 |
|---|---|
| [flash_fwd_kernel.h:132](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L132) | `const int residual_len = params.new_lens;`——残余区有效长度 |
| [flash_fwd_kernel.h:360](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L360) | 构造 `Mask` 时作为残余区的序列长度参与掩码计算 |
| [flash_fwd_kernel.h:373](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L373) / [390](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L390) | 作为 K/V 残余加载的剩余长度谓词(补零区不搬进共享内存) |
| [flash_fwd_kernel.h:401](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L401) / [467](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L467) | `if (params.new_lens == residual_block_size)`——**残余攒满一块,kernel 内原位再量化**的触发条件(u5-l4 的伏笔) |

(本实践是纯源码阅读,不需要 GPU,也无需编译。)

#### 4.1.5 小练习与答案

**练习 1:** 为什么把字段拆成 `Qkv_params` 基类和 `Flash_fwd_params` 派生类两层,而不是一个大结构体?

**答案:** 基类只描述"KV/Q 数据的位置与走法",qpack kernel(它不做注意力,只做量化打包)只需要这些;派生类描述"注意力怎么算"(输出、LSE、缩放、split、mask)。分层让 `flash_qpack_kernel` 的参数类型语义上更小,也延续了上游 FlashAttention 的结构、减少合并冲突。当然两层仍共用同一类型名传入 kernel(宏里写死 `const Flash_fwd_params`),qpack 实例只是不去读派生字段而已。

**练习 2:** 结构体里的 `std::string quant_mode` 会一路传到 GPU 吗?kernel 靠什么知道当前是 2-bit 还是 4-bit?

**答案:** 结构体按值整体拷贝进 kernel 参数空间,字符串成员也随之在内存在场,但 device 代码从不读它。kernel 知道位宽/模式靠的是**模板参数**——host 侧 dispatch(`decode_api.cpp:196-216`)用字符串和 `group_size` 选出编译好的模板实例,配置在编译期固化。这正呼应 u2-l3/u3-l1 的结论:quant_mode/num_bits/group_size 是编译期参数,Python 传入的运行时值必须在 C++ 层完成"运行时 → 模板实例"的映射。

**练习 3:** `set_params_fprop` 开头的 `params = {};` 起什么作用?没有它会怎样?

**答案:** `{}` 是值初始化,把整个结构体清零(指针为 `nullptr`、整数为 0、bool 为 false)。由于结构体在栈上声明且很多字段在特定路径下不赋值(比如 decode 路径的 `k_ptr`/`v_ptr` 及其 stride 被注释掉),没有这次清零,kernel 读到的就是**未初始化的栈脏数据**,产生不可复现的越界;有了它,未填字段至少是安全的空值。这也解释了为什么"某分支被注释"只会导致静默无操作,而不是随机崩溃。

### 4.2 set_params_fprop:decode 路径的参数提取与非对称 stride

#### 4.2.1 概念说明

`set_params_fprop` 是 decode 路径的主填参函数:接收 Python 传来的 26 个参数中与"数据"相关的部分,把它们脱水成裸指针 + stride + 整数,填进 `Flash_fwd_params`。它解决的核心问题是:**kernel 不认识 `at::Tensor`,只认识指针和步长**。

本讲最重要的概念是 stride 字段的**逻辑命名**。四个字段的含义按"逻辑角色"固定:

| 逻辑名 | 含义 |
|---|---|
| `batch_stride` | 换一个 batch 样本跨多少元素 |
| `row_stride` | 沿**序列方向**前进一行(一个 token,或一个打包行/一个分组行)跨多少元素 |
| `head_stride` | 换一个 KV 头跨多少元素 |
| `dim_stride`(仅 params) | 沿 **head_dim 方向**前进(一个通道/一个分组)跨多少元素 |

而"沿序列一格"在物理张量里落在哪个维度,**随布局而变**。回顾 u2-l1 的真实分配(k-channel 模式,`pack_nums = 16/num_bits`):

| 张量 | 形状 | 序列在哪个物理维 |
|---|---|---|
| `k_pack` | \((b,\; s/\text{pack\_nums},\; h,\; d)\) uint16 | dim -3(按打包行) |
| `k_params` | \((b,\; s/g,\; h,\; d)\) fp32 | dim -3(按分组行) |
| `v_pack` | \((b,\; s,\; h,\; d/\text{pack\_nums})\) uint16 | dim -3(按 token,打包沿通道) |
| `v_params` | \((b,\; d/g,\; h,\; s)\) fp32 | **dim -1**(序列维被放到了最后!) |

形状的出处是 [evaluation/test.py:78-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82)(`v_pack`/`v_params` 的分配)与 [evaluation/llama.py:714-722](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L714-L722)(两种 quant_mode 的分支对照)。K 系(序列在 dim -3)与 V 系 params(序列在 dim -1)物理布局相反——**非对称 stride 提取正是为这件事而生**。

#### 4.2.2 核心流程

`set_params_fprop` 的执行顺序(伪代码):

```text
params = {}                            # 清零兜底
填 quant_mode / group_size / is_bf16
填 q/out 指针
填 K_pack/k_params/v_pack/v_params 指针        # 注意 k_ptr/v_ptr 被注释
填 row_stride:  q/k_pack/k_params/v_pack ← stride(-3)
                v_params          ← stride(-1)   # 非对称!
填 dim_stride:  k_params ← stride(-1)
                v_params ← stride(-3)            # 又是反的!
填 head_stride: 全部 ← stride(-2)
非 varlen 时填 batch_stride: 全部 ← stride(0)
                (若 seqlenq_ngroups_swapped: q/o 的 batch_stride 再 ×seqlen_q)
填 cu_seqlens_q/k、seqused_k、p_ptr、softmax_lse_ptr
填维度 b/h/h_k/seqlen_q/seqlen_k/... 与 h_h_k_ratio
算 softmax 缩放(预乘 log2)、dropout 常量、窗口/因果开关
填 is_seqlens_k_cumulative = true、unpadded_lse、seqlenq_ngroups_swapped
```

kernel 侧消费这些 stride 的地址公式(residual kernel 的 K_pack 为例):

\[
\text{offset} \;=\; \underbrace{\text{batch} \cdot S_b^{\text{pack}}}_{\text{批次}} \;+\; \underbrace{n_{\text{block}} \cdot kBlockP \cdot S_r^{\text{pack}}}_{\text{第 } n_{\text{block}} \text{ 个 KV 块}} \;+\; \underbrace{(h_q / r) \cdot S_h^{\text{pack}}}_{\text{GQA 头映射}}
\]

其中每前进一个 KV 块:`k_pack` 走 `kBlockP = kBlockN / pack_num` 个打包行、`k_params` 走 `kBlockK_params = kBlockN / group_size` 个分组行、`v_params` 走 `kBlockN` 个 token。这三个"每块步数"常量定义在 [kernel_traits.h:85-88](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L85-L88):

```cpp
static constexpr int kBlockP            = quant_mode == 1 ? kBlockN / pack_num : kBlockN;
static constexpr int kBlockK_params     = quant_mode == 1 ? kBlockN / group_size : kBlockN;
```

#### 4.2.3 源码精读

**指针提取——decode 路径不需要 FP16 主缓存。** [decode_api.cpp:61-67](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L61-L67):

```cpp
params.q_ptr = q.data_ptr();
// params.k_ptr = k.data_ptr();
params.K_pack_ptr = k_pack.data_ptr();
params.k_params_ptr = k_params.data_ptr();
// params.v_ptr = v.data_ptr();
params.v_pack_ptr = v_pack.data_ptr();
params.v_params_ptr = v_params.data_ptr();
```

调用方 `mha_fwd_kvcache` 传进来的 `kcache_padded`/`vcache_padded` 本身就是未定义的空张量,两行赋值被注释掉——decode 注意力**只吃量化后的缓存**,FP16 的 K/V 一个字节都不读,这正是低比特方案省带宽的代码级证据。残余区的 FP16 数据走另一条路(`knew_ptr`/`vnew_ptr`,见 4.3)。

**非对称 stride 提取——本讲的核心八行。** [decode_api.cpp:68-78](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L68-L78):

```cpp
// All stride are in elements, not bytes.
params.q_row_stride = q.stride(-3);
// params.k_row_stride = k.stride(-3);
params.K_pack_row_stride = k_pack.stride(-3);
params.k_params_row_stride = k_params.stride(-3);
// params.v_row_stride = v.stride(-3);
params.v_pack_row_stride = v_pack.stride(-3);
params.v_params_row_stride = v_params.stride(-1);   // ← V 的序列在最后一维

params.k_params_dim_stride = k_params.stride(-1);
params.v_params_dim_stride = v_params.stride(-3);   // ← V 的分组在 dim -3
```

对照 4.2.1 的表:K 系张量序列在 dim -3,所以 `row = stride(-3)`、(k_params 的)通道维自然连续 `dim = stride(-1) = 1`;`v_params` 形状是 \((b, d/g, h, s)\),序列被放到 dim -1,于是 `row = stride(-1)`、分组维在 dim -3 所以 `dim = stride(-3)`。**字段名相同、物理下标相反**——命名按逻辑角色,提取按实际布局。

**head/batch 两级。** [decode_api.cpp:80-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L80-L86) 所有 head stride 统一取 `stride(-2)`(两种布局里头维都恰好排在倒数第二);[decode_api.cpp:92-105](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L92-L105) 在非 varlen 分支取 `stride(0)` 作 batch stride,并在 `seqlenq_ngroups_swapped` 时把 q/out 的 batch stride 额外乘以 `seqlen_q`(即 ngroups)——这是 u3-l1 讲过的 GQA 重排的配套补偿:q 被 reshape+transpose 后,kernel 以展平的 (b × ngroups) 视角寻址,需要把折叠掉的维度补回跨度里。

**`cu_seqlens_k` 与 `seqlen_k` 的语义差异(学习目标 3)。** 这两个名字极像,语义却完全不同:

- `params.seqlen_k` 是**标量**,由 `mha_fwd_kvcache` 从 `v_pack.size(1)` 得来([decode_api.cpp:372](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L372))。注意这里**没有除以 pack_nums**——因为 V 的序列维不打包(打包沿通道),`v_pack.size(1)` 本来就是 token 数。它表示"已打包主缓存的总长度(按最大批次)"。
- `params.cu_seqlens_k` 是**设备端指针**。`set_params_fprop` 先把它设为调用方传入的 `cu_seqlens_k_d`(本路径为 nullptr);随后在 `mha_fwd_kvcache` 的残余分支里被**覆盖**([decode_api.cpp:473](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L473)),指向 Python 传入的 `opt_seqlens_k` 张量——即 test.py 里 `seqlens_k = torch.full((batch_size,), seqlen_pack, ...)`([evaluation/test.py:124](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L124)),内容是**每个批次的已打包主缓存长度**。同时 [decode_api.cpp:502](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L502) 把 `is_seqlens_k_cumulative` 置 false。
- 真正表示"**残余区有效长度**"的是 `params.new_lens`(标量 int,`Qkv_params` 的成员),在 [decode_api.cpp:462](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L462) 被赋为 Python 传入的 `new_lens`(模型侧即 `cur_residual_len`,[evaluation/llama.py:677](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L677))。而 `seqlen_knew` 是另一个标量 = 残余缓冲的**补零后总长**(恒等于 `residual_block_size`)。

Device 侧的消费证据在 `BlockInfo` 构造函数([block_info.h:15-25](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L15-L25)),注释写得很直白:

```cpp
// If is_seqlens_k_cumulative, then seqlen_k is cu_seqlens_k[bidb + 1] - cu_seqlens_k[bidb].
// Otherwise it's cu_seqlens_k[bidb], i.e., we use cu_seqlens_k to store the sequence lengths of K.
```

decode 路径 `is_seqlens_k_cumulative = false`,于是 `seqlen_k_cache = cu_seqlens_k[bidb]`(本批次已打包长度),且 `sum_s_k = -1` 使地址偏移退化为朴素的 `bidb * batch_stride`([block_info.h:33-37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L33-L37));总注意力长度则是 `actual_seqlen_k = seqlen_k_cache + seqlen_knew`(打包区 + 补零残余区,[block_info.h:23](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L23))。**原版 FA 的 varlen 累积偏移数组,在这里被复用成"每批次缓存长度"数组**——这是阅读 BitDecoding 时最容易踩的语义陷阱。

**kernel 消费侧。** residual kernel 的地址计算([flash_fwd_kernel.h:1316-1332](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1316-L1332))严格套用 4.2.2 的公式,例如 K_pack 与 v_params 两行:

```cpp
const index_t row_offset_k_pack = ... + blockN_idx * kBlockP * params.K_pack_row_stride
                                  + (bidh / params.h_h_k_ratio) * params.K_pack_head_stride;
const index_t row_offset_v_params = ... + blockN_idx * kBlockN * params.v_params_row_stride
                                  + (bidh / params.h_h_k_ratio) * params.v_params_head_stride;
```

注意 `v_params` 那行的块步进乘的是 `kBlockN`(token 数)而不是 `kBlockP`——因为 v_params 的"行"就是 token。随后用这些 offset 构造 CuTe 视图([flash_fwd_kernel.h:1338-1353](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1338-L1353)):

```cpp
Tensor gK_pack   = make_tensor(..., Shape<Int<kBlockP>, Int<kHeadDim_k>>{},
                       make_stride(params.K_pack_row_stride, _1{}));
Tensor gK_params = make_tensor(..., Shape<Int<kBlockK_params>, Int<kHeadDim_k_params>>{},
                       make_stride(params.k_params_row_stride, params.k_params_dim_stride));
Tensor gV_params = make_tensor(..., Shape<Int<kBlockN>, Int<kHeadDim_v_params>>{},
                       make_stride(params.v_params_row_stride, params.v_params_dim_stride));
```

`gV_params` 的形状是 (本块 token 数, 每 token 的通道分组数),第 0 坐标步长是 `v_params_row_stride`(=1)。**这暴露了布局动机:一个 KV 块内同分组的 `kBlockN` 个参数是连续的 float,一次合并访存(coalesced)就能搬完**——u2-l1 说"v_params 序列维放最后一维使同组参数内存连续",在这里兑现。另可对照 splitkv kernel 的同段代码([flash_fwd_kernel.h:705-717](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L705-L717)),它干脆把这两个步长硬编码成 `_1{}`——在 k-channel 连续布局下 `k_params.stride(-1)` 与 `v_params.stride(-1)` 恰好都等于 1,硬编码才成立。

顺带一个从代码可读出的适配缺口:`set_params_fprop` 的提取是**无条件**按 k-channel 语义写的。若启用 k-tensor(K 的 `k_params` 变为 \((b, d/g, h, s)\),序列挪到 dim -1),那么"row 应取 stride(-1)、dim 应取 stride(-3)"正好与现在的提取相反。这与 `decode_api.cpp:207-215` 里 k-tensor splitkv 分支整体被注释、genfile 对应实例化被注释的现状(u3-l1、u7-l3)互相印证:启用 k-tensor 解码不止要解开 dispatch,还要适配这组 stride 的提取逻辑。

#### 4.2.4 代码实践

**实践目标:** 对照 `set_params_fprop`,为 k-channel 模式下手写一张 stride 对照表;解释 `v_params_row_stride` 为何取 `stride(-1)`;并对题面形状做一次"来源一致性"批判检查。

**操作步骤:**

1. 手工推导。对连续张量用 \( \text{stride}(j) = \prod_{i>j} n_i \) 逐级计算题面给定的 `k_pack = (2, 62, 8, 128)` 与 `k_params = (2, 7, 8, 128)`,按 `set_params_fprop` 的提取表达式(batch←stride(0)、row←stride(-3)、head←stride(-2)、dim←stride(-1))填表。
2. 写验证脚本(纯 CPU,不需要 GPU,不需要编译扩展):

```python
# verify_strides.py —— 示例代码:验证本讲 stride 推导(CPU 即可运行)
import torch

# 题面给定的两个练习形状(k-channel 模式下的物理布局)
k_pack   = torch.zeros((2, 62, 8, 128), dtype=torch.uint16)    # (b, s/pack_nums, h, d)
k_params = torch.zeros((2, 7, 8, 128),  dtype=torch.float32)   # (b, s/group_size, h, d)

# V 系(tensor 式布局):打包沿通道,序列维放最后
v_pack   = torch.zeros((2, 496, 8, 32), dtype=torch.uint16)    # (b, s, h, d/pack_nums)
v_params = torch.zeros((2, 4, 8, 496), dtype=torch.float32)    # (b, d/group_size, h, s)

for name, t in [("k_pack", k_pack), ("k_params", k_params),
                ("v_pack", v_pack), ("v_params", v_params)]:
    print(f"{name:9s} shape={tuple(t.shape)} stride={t.stride()}")
```

3. 运行 `python verify_strides.py`,与手填的表核对。
4. 批判检查:这两个题面形状是否可能来自**同一次** `test.py` 式分配(即存在某个 seqlen 与已启用的 group_size 同时满足两个 dim-1 长度)?

**需要观察的现象:** `k_pack` 与 `k_params` 的 row/head(以及 k_params 的 dim)三个 stride 完全相同,只有 batch stride 不同;`v_params` 的 `stride(-1)` 是 1 而 `stride(-3)` 最大。

**预期结果(按 PyTorch 连续张量定义推导,待本地运行核对):**

| 逻辑字段 | 提取表达式 | k_pack (2,62,8,128) | k_params (2,7,8,128) | v_pack (2,496,8,32) | v_params (2,4,8,496) |
|---|---|---|---|---|---|
| batch | `stride(0)` | 63488 | 7168 | 126976 | 15872 |
| row | `stride(-3)`(K 系/V_pack)或 `stride(-1)`(v_params) | 1024 | 1024 | 256 | **1** |
| head | `stride(-2)` | 128 | 128 | 32 | 496 |
| dim | `stride(-1)`(k_params)或 `stride(-3)`(v_params) | —(pack 无此级) | 1 | — | **3968** |

三个关键结论:

1. **row/head 相同不是巧合。** row stride \(=\) 尾部维度之积 \(8 \times 128 = 1024\),只取决于 \((h, d)\),与 dim-1 装的是 62 个打包行还是 7 个分组行无关。这正是"stride 按逻辑角色可复用"的体现:kernel 推进一个块时,对 pack 和 params 用同一套 `块索引 × 块步数 × row_stride` 公式,只是块步数不同(`kBlockP` vs `kBlockK_params`)。
2. **`v_params_row_stride = stride(-1)` 的原因。** V 恒为 tensor 式布局:`v_pack` 沿通道打包、`v_params` 形状为 \((b, d/g, h, s)\),**序列维物理上在最后一维**。而字段名的"row"逻辑上永远指"沿序列前进一个 token"——K 的序列在 dim -3 所以 `row = stride(-3)`,V 的序列在 dim -1 所以 `row = stride(-1)`。代价换来的是收益:`v_params` 沿序列 stride 为 1,同一分组下一个 KV 块的参数是连续 float,kernel 里 `gV_params` 第 0 坐标以步长 1 前进,合并访存友好。
3. **一致性检查的答案:不可能同源。** 4-bit 下 `ceil(s/4)=62` 要求 \(s \in [245, 248]\),而 `ceil(s/g)=7` 在 g=32 时要求 \(s \in [193, 224]\)、g=128 时要求 \(s \in [769, 896]\),均无交集;2-bit 下 `ceil(s/8)=62` 要求 \(s \in [489,496]\),与 g=32/128 的区间同样无交集。所以这两个形状是**独立的练习用形状**,不对应同一次真实分配——做 stride 推导时不必强行让它们对齐。

#### 4.2.5 小练习与答案

**练习 1:** 把头维从 8 改成 16(即 \((2, 62, 16, 128)\)),`k_pack` 的 row stride 变成多少?batch stride 呢?

**答案:** row stride \(= 16 \times 128 = 2048\)(仍是尾部维度之积);batch stride \(= 62 \times 2048 = 126976\)。

**练习 2:** 为什么 `v_pack` 没有 `dim_stride` 字段,而 `v_params` 有?

**答案:** pack 类张量的最后一维就是打包后的 uint16 列,kernel 构造视图时直接写死 `_1{}`(见 `make_stride(params.v_pack_row_stride, _1{})`);而 params 的"沿 head_dim 方向"这一逻辑坐标的物理位置随布局漂移(K 在 dim -1、V 在 dim -3),且 residual kernel 确实把它作为显式步长传入 `make_stride`(flash_fwd_kernel.h:1343、1353),所以需要字段保存。

**练习 3:** decode 一轮中,`params.seqlen_k`、`cu_seqlens_k` 指向的内容、`new_lens` 三者分别对应 Python 侧的什么?

**答案:** `seqlen_k` = `v_pack.size(1)`(已打包主缓存的最大批次 token 数);`cu_seqlens_k` 指向 `opt_seqlens_k` 张量 = 每批次的 `seqlen_pack`(`torch.full((b,), seqlen_pack)`);`new_lens` = `cur_residual_len`(残余区当前有效 token 数)。三者关系:`actual_seqlen_k = cu_seqlens_k[bidb] + seqlen_knew`(补零残余块),而 `new_lens ≤ seqlen_knew` 标记其中真实有效的部分。

### 4.3 set_params_fprop_qpack 与 mha_fwd_kvcache 的补填:new_* 家族与手工 batch stride

#### 4.3.1 概念说明

结构体是"一份定义、多路填充"。本讲涉及三条填充路径,各自点亮字段的不同子集:

- **`set_params_fprop_qpack`**(打包路径):输入是 **FP16 原始 K/V**(所以要启用 `k_ptr`/`v_ptr`),输出是四个 pack/params 张量。因为 Python 侧把 batch 折叠进了 dim0,这里还要**手工重算 batch stride**。
- **`mha_fwd_kvcache` 的残余分支补填**(decode 路径):`set_params_fprop` 之后,如果传入了残余区(`k_`/`v_` 有值),再补上 `knew`/`vnew` 与 **`new_*` 四件套**的指针和四级 stride——`k_pack_new` 等张量是残余攒满一块时 kernel 原位量化写出的新块,布局与主缓存**完全同构**,所以 stride 提取也是完全镜像的。
- **`set_params_splitkv`**:补 `num_splits` 和两个 float 累积缓冲(u3-l3 的主题,本讲略)。

三条路径共用"`params = {};` 清零兜底"的安全网:没被点亮的字段保持 nullptr/0。

#### 4.3.2 核心流程

三条路径的字段填充对照(●=填充,○=注释/留空,-=不适用):

| 字段组 | qpack 路径 | decode 路径 | 说明 |
|---|---|---|---|
| `q_ptr` 及 q stride | - | ● | qpack 不做注意力 |
| `k_ptr`/`v_ptr`(FP16) | ● | ○(注释) | decode 不读 FP16 主缓存 |
| `K_pack`/`k_params`/`v_pack`/`v_params` 指针+stride | ●(作输出) | ●(作输入) | 提取表达式完全相同 |
| `o_ptr`/`softmax_lse_ptr` | - | ● | |
| `knew`/`vnew` 指针+stride | - | ●(残余存在时) | |
| `*_new` 四件套指针+stride | - | ●(残余存在时) | 与主缓存镜像 |
| `new_lens`/`seqlen_knew` | - | ●(残余存在时) | |
| `cu_seqlens_k` | 留 nullptr | 覆盖为每批次打包长度 | |
| `quant_mode`/`group_size` | ● | ● | host dispatch 用 |
| `num_splits`/累积缓冲 | - | ● | u3-l3 |

#### 4.3.3 源码精读

**qpack 的指针:FP16 原始 K/V 回归。** [decode_api.cpp:555-560](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L555-L560) 把六个指针全部赋值(对比 decode 路径的 4.2.3):

```cpp
params.k_ptr = k.data_ptr();
params.K_pack_ptr = k_pack.data_ptr();
params.k_params_ptr = k_params.data_ptr();
params.v_ptr = v.data_ptr();
params.v_pack_ptr = v_pack.data_ptr();
params.v_params_ptr = v_params.data_ptr();
```

[decode_api.cpp:562-577](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L562-L577) 的 stride 提取与 `set_params_fprop` 逐行相同(含同样的非对称:`v_params_row_stride = v_params.stride(-1)`、`v_params_dim_stride = v_params.stride(-3)`)——**同一套布局约定贯穿打包与解码两端**,这是"布局决定索引数学"的又一证据。

**手工 batch stride。** [decode_api.cpp:579-586](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L579-L586):

```cpp
if (page_kv) params.k_batch_stride = k.stride(0);
else params.k_batch_stride = seqlen_k * k.size(-2) * k.size(-1);
```

原因在 Python 侧:[bit_decode_interface.py:21-24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21-L24) 把 \((b, s, h, d)\) 的 `k_cache` reshape 成 \((b \cdot s, h, d)\)——batch 维被折叠进 dim0,张量的 `stride(0)` 变成了 \(h \cdot d\),不再有"batch 跨度"可取。于是 C++ 侧从 `cu_seqlens_k.numel() - 1` 恢复 batch 数([decode_api.cpp:635](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L635)),并手工拼出 `seqlen_k * h * d` 当 batch stride。它与 row stride \(h \cdot d\) 配合仍然成立:把折叠后的行号 \(r = b \cdot s + t\) 展开,\(r \cdot (h d) = b \cdot (s h d) + t \cdot (h d)\),恰好等于"batch 偏移 + 行偏移"两级寻址。

**qpack 路径的 `cu_seqlens_k` 留空。** 调用处传的是 nullptr:[decode_api.cpp:659-670](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L659-L670)(第 666 行 `/*cu_seqlens_k_d=*/nullptr`),于是 [decode_api.cpp:588](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L588) 把指针置空——`cu_seqlens_k` 张量本身只用来数 batch 个数。指针为空时 `BlockInfo` 的 `sum_s_k = -1`,`k_offset` 退回 `bidb * batch_stride`,与上面的手工 batch stride 严丝合缝。

**decode 路径的 new_* 补填。** `mha_fwd_kvcache` 在 `set_params_fprop` 之后、`set_params_splitkv` 之前,若残余输入存在则补一段([decode_api.cpp:440-500](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L440-L500))。先是三个标量与残余 FP16 输入([462-473](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L462-L473)):

```cpp
params.new_lens          = new_lens;
params.seqlen_knew       = seqlen_knew;
params.knew_ptr          = k.data_ptr();
...
params.cu_seqlens_k      = static_cast<int *>(seqlens_k.data_ptr());  // 覆盖!
```

注意 `cu_seqlens_k` 在这里被**覆盖**成 `opt_seqlens_k`(4.2.3 已详述)。接着是 `new_*` 四件套的指针与四级 stride([477-498](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L477-L498)),提取方式与主缓存逐行镜像:

```cpp
params.k_pack_new_row_stride     = k_pack_new.stride(-3);
params.v_params_new_row_stride   = v_params_new.stride(-1);   // 与主缓存同款非对称

params.k_params_new_dim_stride   = k_params_new.stride(-1);
params.v_params_new_dim_stride   = v_params_new.stride(-3);
```

镜像的物理保证来自分配侧:`*_new` 张量就是把主缓存的序列长度换成 `residual_block_size` 再分配一遍([evaluation/llama.py:747-750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L747-L750)),所以 kernel 写新块与读主缓存可以用同一套索引数学。顺带一提,[decode_api.cpp:475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L475) 算出的 `pack_nums` 在本函数后续没有再被使用,是一行残留代码——阅读时不要被它误导。

#### 4.3.4 代码实践

**实践目标:** 亲手验证"三条路径各点亮哪些字段",并验证 `new_*` 张量的 stride 提取确实与主缓存镜像。

**操作步骤:**

1. 打开 [decode_api.cpp:549-599](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L549-L599)(qpack 填参)与 [decode_api.cpp:54-106](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L54-L106)(decode 填参),把 4.3.2 的对照表补成"具体行号"版:每个 ● 标注赋值语句所在的行。
2. 用一段 CPU 脚本(示例代码)按 llama.py 的分配方式构造主缓存与 `new` 块,打印两者的 stride 对照:

```python
# verify_new_strides.py —— 示例代码:验证 new_* 与主缓存布局镜像(CPU 即可)
import torch

b, h, d, s, g, p, r = 1, 32, 128, 896, 32, 4, 128   # 4-bit, k-channel, group=32

main_tensors = {
    "k_pack":   torch.zeros((b, s // p, h, d), dtype=torch.uint16),
    "k_params": torch.zeros((b, s // g, h, d), dtype=torch.float32),
    "v_pack":   torch.zeros((b, s, h, d // p), dtype=torch.uint16),
    "v_params": torch.zeros((b, d // g, h, s), dtype=torch.float32),
}
new_tensors = {   # llama.py:747-750 的分配方式:序列长度换成 residual_block_size
    "k_pack_new":   torch.zeros((b, r // p, h, d), dtype=torch.uint16),
    "k_params_new": torch.zeros((b, r // g, h, d), dtype=torch.float32),
    "v_pack_new":   torch.zeros((b, r, h, d // p), dtype=torch.uint16),
    "v_params_new": torch.zeros((b, d // g, h, r), dtype=torch.float32),
}
for (mn, mt), (nn, nt) in zip(main_tensors.items(), new_tensors.items()):
    print(f"{mn:10s}{tuple(mt.stride())}  |  {nn:13s}{tuple(nt.stride())}")
```

**需要观察的现象:** 每一对主缓存/新块的 row、head、dim 三级 stride 应该**数值完全相同**(因为尾部维度都是 \((h, d)\) 或 \((h, r)\) 的同构布局),不同的只有 batch stride(序列长度不同)。

**预期结果(推导,待本地运行核对):** 例如 `k_pack` 的 stride 为 \((s/p \cdot h \cdot d,\; h \cdot d,\; d,\; 1)\),`k_pack_new` 为 \((r/p \cdot h \cdot d,\; h \cdot d,\; d,\; 1)\)——row 同为 \(h d = 4096\),head 同为 128;`v_params` 与 `v_params_new` 的 row 同为 `stride(-1) = 1`、dim 同为 `stride(-3)`。这就是"镜像提取"在数值上的含义:kernel 用同一套 `块索引 × 块步数 × row_stride` 公式,既能读主缓存也能写新块。若你把脚本中某个张量的分配维度抄错(比如把 `v_params_new` 写成 \((b, r, h, d/g)\)),row/dim stride 就不再与主缓存配对——这正是一次自查布局理解的好机会。

#### 4.3.5 小练习与答案

**练习 1:** qpack 路径如果不手工算 `k_batch_stride = seqlen_k * h * d`,而是直接用 `k.stride(0)`,会发生什么?

**答案:** 折叠后的 `k` 形状是 \((b \cdot s, h, d)\),`k.stride(0) = h \cdot d` 是**行**跨度。把它当 batch 跨度用,kernel 里 `bidb * batch_stride` 只前进 \(h d\) 个元素(即一个 token),后续所有 batch 的读取都会落在错误位置——但不会越界崩溃,只会得到错误的量化结果,属于最难排查的一类静默错误。

**练习 2:** `v_params_new_row_stride` 取 `stride(-1)`、`v_params_new_dim_stride` 取 `stride(-3)`,与主缓存的提取完全一样。为什么可以一样?

**答案:** 因为 `v_params_new` 的分配(llama.py:750)就是主缓存 `v_params` 布局 \((b, d/g, h, s)\) 把 \(s\) 换成 `residual_block_size` 的同构复制,序列维仍在 dim -1、分组维仍在 dim -3。布局相同,逻辑角色相同的字段自然取相同表达式的 stride。

**练习 3:** decode 路径中 `params.cu_seqlens_k` 先在 `set_params_fprop` 里被赋 nullptr,之后又被覆盖。两次赋值分别有什么用?

**答案:** 第一次(set_params_fprop 的入参 `cu_seqlens_k_d` 为 nullptr)是"本路径没有 varlen 偏移数组"的声明,配合 batch stride 使用朴素寻址;第二次(473 行)把它覆盖为残余输入自带的 `seqlens_k` 张量,并靠 502 行的 `is_seqlens_k_cumulative = false` 告诉 `BlockInfo`"这个数组存的是每批次长度而不是累积偏移"。最终效果:`seqlen_k_cache = cu_seqlens_k[bidb]` 给出本批次已打包长度,地址偏移仍走 `bidb * batch_stride`。

## 5. 综合实践

**任务:用 `evaluation/test.py` 的默认配置,把"Python 分配 → stride 提取 → kernel 地址计算"整条链走一遍数值。**

test.py 的默认参数([evaluation/test.py:40-57](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L57)):`quant_mode="k-channel"`、`num_bits=4`、`group_size=32`、`residual_block_size=128`、\(b=1\)、\(h = h_k = 32\)、\(d=128\)、`seqlen_k=1024`。由于 \(1024 \bmod 128 = 0\),残余区初始为空,`seqlen_k_pack = 1024`。

**步骤 1:写出 8 个张量的形状与四级 stride 表**(4 个主缓存按 [test.py:78-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82) 分配,4 个 `new` 块按 [test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113) 分配)。

**步骤 2:用脚本核对**(把 4.3.4 的脚本参数换成 \(b=1, h=32, d=128, s=1024, g=32, p=4, r=128\))。预期(推导,待本地核对):

| 张量 | 形状 | batch | row | head | dim |
|---|---|---|---|---|---|
| k_pack | (1, 256, 32, 128) | 1048576 | 4096 | 128 | — |
| k_params | (1, 32, 32, 128) | 131072 | 4096 | 128 | 1 |
| v_pack | (1, 1024, 32, 32) | 1048576 | 1024 | 32 | — |
| v_params | (1, 4, 32, 1024) | 131072 | **1** | 1024 | **32768** |
| k_pack_new | (1, 32, 32, 128) | 131072 | 4096 | 128 | — |
| k_params_new | (1, 4, 32, 128) | 16384 | 4096 | 128 | 1 |
| v_pack_new | (1, 128, 32, 32) | 131072 | 1024 | 32 | — |
| v_params_new | (1, 4, 32, 128) | 16384 | **1** | 128 | **4096** |

**步骤 3:代入 kernel 的地址公式。** dispatch 处固定 `kBlockN = 256`([flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137)),于是 `kBlockP = 256/4 = 64`、`kBlockK_params = 256/32 = 8`。对 `bidb=0, bidh=0, blockN_idx=1`(第二个 KV 块,覆盖 token 256-511),按 [flash_fwd_kernel.h:1316-1332](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1316-L1332) 计算:

\[
\text{offset}_{k\_pack} = 1 \times 64 \times 4096 = 262144,\quad
\text{offset}_{k\_params} = 1 \times 8 \times 4096 = 32768,
\]
\[
\text{offset}_{v\_pack} = 1 \times 256 \times 1024 = 262144,\quad
\text{offset}_{v\_params} = 1 \times 256 \times 1 = 256.
\]

**步骤 4:解释你看到的规律。** (a) `k_pack` 与 `v_pack` 的块起点偏移数值相同——两种 pack 的元素总数一致(\(s \cdot h \cdot d / p\)),且都按"块内首 token"对齐;(b) `v_params` 的偏移只有 256,因为它的"行"是 token 且行跨度为 1;(c) `k_pack_new` 家族的 row/head stride 与主缓存完全相同,再次印证镜像布局。若在 GPU 机器上,还可在 test.py 的 decode 循环里加一行 `print(k_pack.stride(), v_params.stride())`(残余攒满触发 `update_pack` 前后各打印一次)验证拼接后 stride 不变(`torch.cat` 沿 dim0/拼接维返回的新张量仍是连续布局)。

## 6. 本讲小结

- `Flash_fwd_params`(公有继承 `Qkv_params`)是贯穿 CPU 填参与 qpack/splitkv/residual/combine 四类 GPU kernel 的唯一参数包;为低比特缓存新增的核心字段是 `K_pack`/`k_params`/`v_pack`/`v_params` 及其 `new` 变体的指针与四级 stride,外加 `quant_mode`/`group_size`/`new_lens`。
- stride 字段按**逻辑角色**命名(row=沿序列一格、head=换头、dim=沿 head_dim 方向);k-channel 下 K 系序列在 dim -3,而 `v_params` 布局为 \((b, d/g, h, s)\) 序列在 dim -1,于是出现了 `v_params_row_stride = stride(-1)`、`v_params_dim_stride = stride(-3)` 的非对称提取,收益是残余块参数沿序列连续、合并访存友好。
- `set_params_fprop` 以 `params = {};` 清零兜底,decode 路径注释掉 FP16 主缓存指针(只吃量化缓存),qpack 路径则启用 `k_ptr`/`v_ptr` 并因 Python 折叠了 batch 而**手工重算 batch stride**。
- `cu_seqlens_k` 在 decode 路径被复用为"每批次已打包主缓存长度"数组(`is_seqlens_k_cumulative=false`),`seqlen_k` 是打包区总长度标量,真正的残余有效长度是 `new_lens`(device 侧参与掩码、加载谓词与"攒满即再量化"触发)。
- `mha_fwd_kvcache` 补填的 `new_*` 四件套与主缓存布局完全同构、stride 提取逐行镜像;`quant_mode` 字符串只在 host dispatch 消费,配置真正生效靠模板参数。
- 提取逻辑是无条件按 k-channel 语义写的——启用被注释的 k-tensor 解码分支时,这组 stride 提取(以及 kernel 内硬编码的 `_1{}`)是需要同步适配的位置之一。

## 7. 下一步学习建议

本讲结束于 `set_params_splitkv(params, ...)` 这个被我们一笔带过的调用——下一讲 **u3-l3(split 数量启发式与中间缓冲分配)** 正好展开它:`num_splits_heuristic` 如何用 occupancy 波形效率 \(\text{eff} = n_{\text{waves}} / \lceil n_{\text{waves}} \rceil\) 选择 split 数、为什么 `params.num_splits` 要额外加 1 留给残余 kernel、以及 `softmax_lseaccum`/`out_accum` 两个 float 缓冲何时分配。之后第四单元下潜 qpack kernel 本体(`csrc/bit_decode/src/include/qpack.h`),第五单元回到本讲反复引用的 `flash_fwd_kernel.h` 主循环——届时本讲的地址公式会成为每一步推导的地基。若想巩固本讲,建议把 5 的综合实践表扩写到 `group_size=128` 与 2-bit 两档配置再算一遍。
