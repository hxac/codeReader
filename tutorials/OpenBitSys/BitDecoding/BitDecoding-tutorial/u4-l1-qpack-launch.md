# QPack kernel 启动路径：从 run_kvcache_qpack 到 GPU 网格

## 1. 本讲目标

前三单元我们已经走通了「Python 接口 → pybind11 → Flash_fwd_params → split 启发式」这条绑定链，但一直停在 `run_mha_fwd` / `run_kvcache_qpack` 的门口。本讲推开其中一扇门：**量化打包（QPack）kernel 的完整启动路径**。学完本讲你应该能够：

1. 画出从 Python 的 `kvcache_pack_int` 到 GPU 上 `flash_qpack_kernel` 被调度执行的完整调用链，并说出链上每一环所在的文件与行号。
2. 解释 `run_kvcache_qpack` 的 dispatch if 链、`genfile/*.cu` 的显式模板实例化、`setup.py` 的源文件列表三者为什么必须严格成对。
3. 推导 `Flash_qpack_traits` 的全部关键编译期常量（`kBlockN`、`kBlockP`、`kHeadDim_pack`、`num_params` 等），并说明它与解码 kernel 的 `Flash_fwd_kernel_traits` 在共享内存布局上的本质差异。
4. 给定 batch、序列长度、头数、位宽，手工算出 kernel 的 grid 三维、block 线程数、`SharedStorage` 字节大小，并解释为什么这里必须调用 `cudaFuncSetAttribute`。

本讲不深入量化数学本身（组内 max/min 归约、scale/zero 计算、LOP3 反量化），那些留给第四单元后两讲（u4-l2、u4-l3）与第五单元。

## 2. 前置知识

### 2.1 显式模板实例化（explicit template instantiation）

C++ 模板函数只有在被「具体类型组合」调用时才会生成机器码。`run_kvcache_qpack_<T, Headdim, quant_mode, num_bits, group_size>` 是一个五参数模板，理论上有无数种组合，编译器不可能全部生成。项目的做法是：

- 在头文件里只放**声明**（不生成代码）；
- 在专门的 `.cu` 文件里写 `template<> void run_kvcache_qpack_<...>(...) { ... }`，**强制为特定组合生成代码**，这就是显式实例化；
- 运行时 dispatch 的 `if (params.group_size == 128)` 分支，只有命中「已被实例化的组合」才是合法调用。

这套机制决定了：**新增一个量化配置 = 同时改 dispatch 分支 + genfile 实例化 +（必要时）setup.py 源列表**，三处缺一不可（这正是 u7-l3 扩展实践的主题）。

### 2.2 CUDA kernel 启动三要素

`kernel<<<grid, block, smem_size, stream>>>(...)` 中：

- **grid**：三维 block 网格 `(gridDim.x, gridDim.y, gridDim.z)`，kernel 内用 `blockIdx.x/y/z` 读取自己负责哪块数据；
- **block**：每个 block 的线程数，本项目 qpack kernel 固定 `kNWarps(4) × 32 = 128` 线程；
- **smem_size**：**动态**共享内存字节数。kernel 内用 `extern __shared__ char smem_[]` 接收（[flash_fwd_kernel.h:1281](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1281)）。

### 2.3 48 KB 动态共享内存限制与 `cudaFuncSetAttribute`

CUDA 规定：不额外声明时，每个 block 的动态共享内存上限是 **48 KiB**；Ampere(sm_80)/Hopper(sm_90) 可以通过

```cpp
cudaFuncSetAttribute(kernel, cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size);
```

**逐 kernel 地**把上限抬高（sm_80 每 block 最高约 163 KiB，sm_90 约 227 KiB）。本讲的 qpack kernel 共享内存远超 48 KiB，所以启动前必须做这个"opt-in"声明，否则启动直接失败。为什么抬高不是默认行为？因为共享内存是从每个 SM 的物理容量里划出来的，占用越大，同驻一个 SM 的 block 数（occupancy）越少，CUDA 让程序员显式权衡。

### 2.4 需要回顾的前几讲结论

- u2-l1：`pack_num = 16 / num_bits`（一个 uint16 装 4 个 int4 或 8 个 int2）；k-channel 模式下 `k_pack=(b, s/pack, h, d)`、`k_params=(b, s/g, h, d)`。
- u3-l1：`kvcache_qpack` 的 dispatch if 链落空会**静默无操作**；Python 侧把 batch 折叠进序列维（`K_unpad = k_cache.reshape(b*s, h, d)`），C++ 须从 `cu_seqlens_k.numel()-1` 恢复 batch 数。
- u3-l2：`Flash_fwd_params` 结构体是 CPU 与 GPU 之间唯一的参数载体，经 `__grid_constant__` 按值传入 kernel。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L12-L45) | Python 入口 | batch 折叠、按 `num_bits` 分流到 `kvcache_pack_int2/4` |
| [csrc/bit_decode/decode_api.cpp](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L602-L685) | pybind11 绑定层 | `kvcache_qpack` host 包装、`set_params_fprop_qpack`、`run_kvcache_qpack` dispatch |
| [csrc/bit_decode/src/include/flash.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L203-L205) | 前向声明 | `run_kvcache_qpack_` 模板声明（只声明不定义） |
| [csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu#L7-L18) | 显式实例化 | 4-bit 的两个活跃实例 |
| [csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu#L7-L18) | 显式实例化 | 2-bit 的两个活跃实例 |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L176-L206) | 启动模板 | `run_kvcache_qpack_hdim128`、`run_flash_qpack`、`flash_qpack_kernel` 定义 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L450-L574) | 编译期骨架 | `Flash_qpack_traits` 常量与 `SharedStorage` |
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1636-L1646) | device kernel | `compute_qpack` 与 `compute_qpack_1rowblock` |
| [setup.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L129-L136) | 构建脚本 | 哪些 `.cu` 参与编译 |

注意：`csrc/bit_decode/src/flash_api.h` 里有一份与 `decode_api.cpp` 几乎相同的 `run_kvcache_qpack`（[flash_api.h:218-233](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L218-L233)），但 `setup.py` 只编译 `decode_api.cpp`，`flash_api.h` 是上游 FlashAttention 遗留副本，读代码时不要被它干扰。

## 4. 核心概念与源码讲解

### 4.1 从 `kvcache_pack_int` 到 `run_kvcache_qpack_`：dispatch 与显式实例化的配对

#### 4.1.1 概念说明

QPack 是 prefill 阶段的"打包机"：输入 FP16 的 K/V，输出量化打包后的 `k_pack/k_params/v_pack/v_params`（布局见 u2-l1）。本模块只关心**主机侧（CPU）如何把一次 Python 调用路由到正确的模板实例**。关键在于：`num_bits`、`quant_mode`、`group_size` 都是**编译期模板参数**，而 Python 传来的却是运行期字符串/整数，所以中间必须有一张"运行期值 → 编译期实例"的路由表，这张表由三份代码共同构成：dispatch if 链、genfile 显式实例化、setup.py 源列表。

#### 4.1.2 核心流程

完整调用链（→ 表示函数调用）：

```text
Python: kvcache_pack_int(k_cache, k_pack, k_params, v_cache, v_pack, v_params, ...)
  │  reshape 折叠 batch: (b, s, h, d) → (b*s, h, d)
  │  按 num_bits 分流
  ▼
pybind11: bit_decode_cuda.kvcache_pack_int4 / int2        (= kvcache_qpack<4>/<2>)
  │  校验 dtype/device/contiguous
  │  batch_size = cu_seqlens_k.numel() - 1   （恢复被折叠的 batch）
  │  set_params_fprop_qpack(... )             （填 Flash_fwd_params，手工算 batch stride）
  │  if (max_seqlen_k > 0)
  ▼
run_kvcache_qpack<num_bits>(params, stream)               （dispatch if 链）
  │  quant_mode=="k-channel" && group_size==128/32 → 命中
  ▼
run_kvcache_qpack_<half_t, 128, 1, num_bits, group_size>  （仅声明；定义在 genfile）
  ▼
run_kvcache_qpack_hdim128<T, quant_mode, num_bits, group_size>
  │  kBlockN = num_bits==4 ? 128 : 256
  ▼
run_flash_qpack<Flash_qpack_traits<128, kBlockN, 4, ...>>  （4.3 节）
  ▼
flash_qpack_kernel<<<grid, 128, smem, stream>>>            （GPU）
```

模板参数顺序固定为 `<T, Headdim, quant_mode, num_bits, group_size>`（承接 u1-l2 的结论），`quant_mode` 用整数 `1` 表示 k-channel、`0` 表示 k-tensor。

#### 4.1.3 源码精读

**第一环：Python 折叠 batch 并按位宽分流。**

[bit_decode/bit_decode_interface.py:21-34](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21-L34)：

```python
batch_size, seqlen_k, nheads_k, d = k_cache.shape
K_unpad = k_cache.reshape(batch_size * seqlen_k, nheads_k, d)
V_unpad = v_cache.reshape(batch_size * seqlen_k, nheads_k, d)
if num_bits == 4:
    bit_decode_cuda.kvcache_pack_int4(K_unpad, k_pack, k_params, ...)
```

这段代码做了两件事：把 `(b, s, h, d)` 重排成 `(b*s, h, d)`（varlen 风格的扁平张量），以及把编译期参数 `num_bits` 变成函数名后缀 `int4/int2`。折叠 batch 的后果由下一环兜底。

**第二环：`kvcache_qpack` host 包装——恢复 batch、填参数、守卫启动。**

[csrc/bit_decode/decode_api.cpp:635-637](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L635-L637)：

```cpp
const int batch_size  = cu_seqlens_k.numel() - 1;
int num_heads         = paged_KV ? sizes[2] : sizes[1];
const int head_size   = paged_KV ? sizes[3] : sizes[2];
```

因为 `K_unpad` 已是三维张量，`k.size(0)` 不再是 batch，只能从 `cu_seqlens_k`（长度 b+1 的累积长度向量）反推 batch 数——这是 u3-l1 已建立的事实，此处看到它的消费点。随后 [decode_api.cpp:659-670](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L659-L670) 调用 `set_params_fprop_qpack` 填 `Flash_fwd_params`，[decode_api.cpp:679-682](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L679-L682) 是启动守卫：

```cpp
if (max_seqlen_k > 0) {
    auto stream = at::cuda::getCurrentCUDAStream().stream();
    run_kvcache_qpack<num_bits>(params, stream);
}
```

`max_seqlen_k` 是 Python 传入的 `seqlen_k`（每序列长度），也是后面 grid 计算用的 `params.seqlen_k`。

`set_params_fprop_qpack` 里有个值得注意的细节——[decode_api.cpp:579-586](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L579-L586)：

```cpp
if (page_kv) params.k_batch_stride = k.stride(0);
else params.k_batch_stride = seqlen_k * k.size(-2) * k.size(-1);
```

非 paged 时 `k.stride(0)` 是**每 token** 的步长（h×d），不能当 batch 步长用，所以手工乘上 `seqlen_k` 重算。这是"折叠 batch"决策的第二个补救点。

**第三环：dispatch if 链。**

[csrc/bit_decode/decode_api.cpp:219-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L219-L238)：

```cpp
template <int num_bits>
void run_kvcache_qpack(Flash_fwd_params &params, cudaStream_t stream) {
    if (params.quant_mode == "k-channel") {
        if (params.group_size == 32) {
            run_kvcache_qpack_<cutlass::half_t, 128, 1, num_bits, 32>(params, stream);
        } else if (params.group_size == 64) {
            // run_kvcache_qpack_<cutlass::half_t, 128, 1, num_bits, 64>(params, stream);
        } else if (params.group_size == 128) {
            run_kvcache_qpack_<cutlass::half_t, 128, 1, num_bits, 128>(params, stream);
        }
    } else { /* k-tensor 三个分支全部被注释 */ }
}
```

当前仓库 qpack 路径只活了 **k-channel × group_size∈{32,128}** 两个分支（`num_bits` 由外层模板给出 2/4 两种），其余全部被注释。`flash.h` 只给出声明（[flash.h:205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L205)），真正定义在 genfile。

**第四环：genfile 显式实例化。**

[csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu:7-18](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu#L7-L18)：

```cpp
template<>
void run_kvcache_qpack_<cutlass::half_t, 128, 1, 4, 32>(Flash_fwd_params &params, cudaStream_t stream) {
    run_kvcache_qpack_hdim128<cutlass::half_t, 1, 4, 32>(params, stream);
}
template<>
void run_kvcache_qpack_<cutlass::half_t, 128, 1, 4, 128>(Flash_fwd_params &params, cudaStream_t stream) {
    run_kvcache_qpack_hdim128<cutlass::half_t, 1, 4, 128>(params, stream);
}
```

2-bit 文件结构完全对称（[flash_qpack_hdim128_fp16_sm80_2bit.cu:7-18](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu#L7-L18) 实例化 `<1, 2, 128>` 与 `<1, 2, 32>`）。两个文件合计 4 个活跃实例，与 dispatch 的 2 个分支 × `num_bits∈{2,4}` 严格一一对应；文件内其余组合（group_size=64、k-tensor）均以注释形式保留。这两个 `.cu` 都出现在 [setup.py:132-133](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L129-L136) 的源列表里，"拆成多个 .cu 分别实例化"正是 u1-l2 讲过的并行编译策略。

#### 4.1.4 代码实践：验证 dispatch 落空的后果

1. **实践目标**：亲眼确认"dispatch if 链落空 = 静默无操作"，理解为什么调错参数不报错但结果全错。
2. **操作步骤**（有 GPU 的机器，基于 u2-l3 的接口签名）：
   ```python
   # 示例代码：group_size=64 是 dispatch 未启用的组合
   import torch, bit_decode
   b, s, h, d = 1, 256, 4, 128
   k = torch.randn(b, s, h, d, dtype=torch.float16, device="cuda")
   k_pack   = torch.zeros(b, s // 4, h, d, dtype=torch.int16, device="cuda")
   k_params = torch.zeros(b, s // 64, h, d, dtype=torch.float32, device="cuda")
   v, v_pack, v_params = k.clone(), k_pack.clone(), k_params.clone()
   cu = torch.arange(0, (b + 1) * s + 1, s, dtype=torch.int32, device="cuda")
   bit_decode.kvcache_pack_int(k, k_pack, k_params, v, v_pack, v_params,
                               cu_seqlens_k=cu, seqlen_k=s,
                               quant_mode="k-channel", group_size=64, num_bits=4)
   print("k_pack 全零？", bool((k_pack == 0).all()))
   ```
3. **需要观察的现象**：程序正常返回、无任何异常，但 `k_pack` 保持全零——没有任何 kernel 被启动。
4. **预期结果**：`True`。因为 `run_kvcache_qpack` 中 `group_size==64` 分支是注释，`if/else if` 链整体落空，函数体什么都没做。把 `group_size` 改成 128 再跑，`k_pack` 应出现非零值。
5. 若无 GPU：直接对照 [decode_api.cpp:219-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L219-L238) 推理即可得出同样结论（此路径可纯静态验证，无需标注"待本地验证"的部分仅限实际运行观察）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run_kvcache_qpack` 里 `group_size == 64` 的注释解开，但不动 genfile，重新编译会发生什么？
**答案**：链接错误（undefined reference to `run_kvcache_qpack_<cutlass::half_t, 128, 1, 4, 64>`）。因为 dispatch 里出现了对模板实例的调用，但没有任何编译单元为它生成定义。必须同时在 genfile 里补上对应的显式实例化。

**练习 2**：为什么 `quant_mode` 在 dispatch 里是字符串比较（`params.quant_mode == "k-channel"`），传到 kernel 时却变成了模板整数参数 1？
**答案**：字符串只存在于 CPU 侧的 host 代码里用于路由；一旦选中分支，`run_kvcache_qpack_<..., 1, ...>` 的模板实参 `1` 就把量化模式固化进类型系统，kernel 内所有依赖 `quant_mode` 的分支（如布局选择）都变成了编译期 `constexpr`，可以在编译期展开、零运行时开销。

**练习 3**：`kvcache_qpack` 为什么不能直接用 `k.size(0)` 当 batch 数？
**答案**：Python 侧已把 `(b, s, h, d)` reshape 成 `(b*s, h, d)`，`k.size(0)` 是 `b*s`；C++ 只能从 `cu_seqlens_k.numel() - 1` 恢复 b（[decode_api.cpp:635](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L635)）。

### 4.2 `Flash_qpack_traits`：qpack kernel 的编译期骨架

#### 4.2.1 概念说明

CuTe/CUTLASS 风格的 kernel 都配一个 "traits" 结构体：把模板参数换算成一堆 `constexpr` 常量（tile 尺寸、打包后维度、分组数），再由这些常量推导共享内存布局（`SmemLayout*`）、全局内存拷贝模式（`GmemTileCopy*`）和 `SharedStorage` 总大小。可以说 **traits 就是 kernel 的"编译期身份证"**：grid 形状、线程数、smem 字节数全部由它决定。

`Flash_qpack_traits` 是 qpack kernel 专用的简化版 traits。和解码用的 `Flash_fwd_kernel_traits` 相比，它砍掉了注意力计算所需的一切：没有 Q 的 smem、没有 softmax 累加器 smem、没有 Swizzle 打包布局、没有喂 MMA 的 LDSM 拷贝。因为**打包 kernel 只做"读 FP16 → 归约 → 量化 → 写 uint16"，不做矩阵乘**。学习目标里说的"kBlockM=32、简化布局"正是指：它的 `Base` 把 `kBlockM` 写死为 32（[kernel_traits.h:451](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L450-L451)，虽然 qpack 根本没有 Q tile，这个 32 只是为了复用基类模板签名），而布局一律采用朴素的 `make_layout` 行主序。

#### 4.2.2 核心流程

traits 的推导链条：

```text
模板参数 (kHeadDim_, kBlockN_, kNWarps_, quantmode_, num_bits_, group_size_)
   │
   ├─ pack_num = 16 / num_bits                       （u2-l1：一个 uint16 装几个值）
   ├─ kBlockP = quant_mode==1 ? kBlockN/pack_num : kBlockN   （打包后 K 的行数）
   ├─ kHeadDim_pack = kHeadDim / pack_num            （打包后 V 的列宽）
   ├─ kHeadDim_k = quant_mode==1 ? kHeadDim : kHeadDim_pack  （k-channel 时 K 不沿 d 压缩）
   ├─ num_params = kBlockN_pack / group_size         （每个 tile 内的量化分组数）
   ▼
SmemLayoutKV / SmemLayoutKPack / SmemLayoutVPack / SmemLayoutReduce_tmp
   ▼
struct SharedStorage { smem_K; smem_V; smem_Kpack; smem_Vpack; smem_reduce_tmp; }
   ▼
kSmemSize = sizeof(SharedStorage)                    （启动时传给 kernel<<< >>>）
```

#### 4.2.3 源码精读

**常量区。** [csrc/bit_decode/src/include/kernel_traits.h:459-487](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L459-L487)：

```cpp
static constexpr int kBlockN           = kBlockN_;
static constexpr int kBlockN_pack      = num_bits   == 4 ? 128 : 256;
static constexpr int kBlockP           = quant_mode == 1 ? kBlockN / pack_num : kBlockN;
static constexpr int kBlockK_params    = quant_mode == 1 ? kBlockN / group_size : kBlockN;
static constexpr int kHeadDim          = kHeadDim_;
static constexpr int kHeadDim_pack     = kHeadDim / pack_num;
static constexpr int kHeadDim_k        = quant_mode == 1 ? kHeadDim : kHeadDim_pack;
static constexpr int kHeadDim_k_params = quant_mode == 1 ? kHeadDim : kHeadDim / group_size;
static constexpr int kHeadDim_v_params = kHeadDim / group_size;
...
static constexpr int num_params = kBlockN_pack / group_size;
```

注意与解码 traits 的关键差别：k-channel（`quant_mode==1`）时 **K 沿序列维度打包**（`kBlockN → kBlockN/pack_num` 行），而 V 沿 head_dim 打包（`kHeadDim → kHeadDim/pack_num` 列）——这正是 u2-l1 讲过的两种张量布局在编译期常量上的体现。另一处细节：qpack 的 `kBlockN` 由上层 `run_kvcache_qpack_hdim128` 指定为 `num_bits==4 ? 128 : 256`，恰等于 `kBlockN_pack`，即**qpack kernel 每个 tile 恰好处理一个"打包块"**（等于 `residual_block_size`，u2-l2 讲过它的由来）。

**共享内存布局区。** [csrc/bit_decode/src/include/kernel_traits.h:499-538](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L499-L538)：

```cpp
using SmemLayoutKV = decltype(tile_to_shape(          // FP16 K 暂存：(kBlockN, kHeadDim)
    SmemLayoutAtomK_tiled{}, Shape<Int<kBlockN>, Int<kHeadDim>>{}));
using SmemLayoutKPack = decltype(                     // 打包 K：朴素行主序，无 Swizzle
    make_layout(make_shape(Int<kBlockP>{}, Int<kHeadDim_k>{}),
                make_stride(Int<kHeadDim_k>{}, _1{})));
using SmemLayoutVPack = decltype(tile_to_shape(       // 打包 V：(kBlockN, kHeadDim_pack)
    SmemLayoutAtomV{}, Shape<Int<kBlockN>, Int<kHeadDim_pack>>{}));
using SmemLayoutReduce_tmp = decltype(                // 归约暂存：32×32
    make_layout(make_shape(Int<32>{}, Int<32>{}), make_stride(Int<32>{}, _1>{})));
```

对比解码 traits 的 `SmemLayoutKPack`（[kernel_traits.h:153-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L153-L160)）：那边用 `composition(Swizzle<kSwizzle,3,3>{}, ...)` 包了一层 swizzle，因为解码 kernel 要用 LDSM 指令从打包 smem 里取出规则碎片喂 Tensor Core，朴素布局会产生 bank conflict；qpack kernel 的打包 smem 只被 `DefaultCopy` 顺序写出（[kernel_traits.h:540-541](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L540-L541) 的 `R2SCopyAtomPack`），不需要 swizzle。`SmemLayoutReduce_tmp` 是 4.2 之外的下一讲（u4-l2）主角——归约原语的跨 warp 暂存区。

**SharedStorage。** [csrc/bit_decode/src/include/kernel_traits.h:543-551](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L543-L551)：

```cpp
struct SharedStorage
{
    array_aligned<Element, cosize_v<SmemLayoutKV>> smem_K;        // FP16 K 输入
    array_aligned<Element, cosize_v<SmemLayoutKV>> smem_V;        // FP16 V 输入
    array_aligned<ElementKVPack, cosize_v<SmemLayoutKPack>> smem_Kpack;   // 打包 K 输出
    array_aligned<ElementKVPack, cosize_v<SmemLayoutVPack>> smem_Vpack;   // 打包 V 输出
    array_aligned<Element, cosize_v<SmemLayoutReduce_tmp>> smem_reduce_tmp; // 归约暂存
};
static constexpr int kSmemSize = int(sizeof(SharedStorage));
```

与解码 kernel 的 `SharedStorage`（[kernel_traits.h:284-297](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L284-L297)，含 `smem_Q/smem_Kpack/smem_Kparams/smem_Vpack/smem_Vparams/smem_acc` 六个数组）对比成下表：

| 维度 | `Flash_fwd_kernel_traits`（解码） | `Flash_qpack_traits`（打包） |
| --- | --- | --- |
| 用途 | Q·K MMA + softmax + P·V MMA | 读 FP16 → 量化 → 打包写出 |
| Base 的 kBlockM | 16（splitkv 路径写死，[launch_template.h:132](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L132)） | 32（Base 默认实参写死，[kernel_traits.h:451](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L451)） |
| SharedStorage 数组 | 6 个：Q、Kpack、Kparams、Vpack、Vparams、acc | 5 个：K(FP16)、V(FP16)、Kpack、Vpack、reduce_tmp |
| Q / acc smem | 有 | 无（不计算注意力） |
| 打包 smem 是否 Swizzle | 是（LDSM 喂 MMA 防 bank conflict） | 否（DefaultCopy 顺序写出） |
| gmem→smem 拷贝 | `SM80_CP_ASYNC_CACHEGLOBAL`（cp.async 异步预取） | 同样用 cp.async 读 FP16 KV（[kernel_traits.h:562-565](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L562-L565)） |
| MMA 定义 | `TiledMma`/`TiledMmaKV_i4` 真正参与两次 gemm | 定义了 `TiledMma`/`TiledMmaK_i4`（[kernel_traits.h:489-497](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L489-L497)）但 kernel 主体一次 gemm 都不调 |

最后一行值得强调：traits 结构体里定义了 MMA 类型不代表 kernel 用了它——`compute_qpack_1rowblock`（本讲 4.4 节）的主体里没有任何 `flash::gemm*` 调用（全部 gemm 调用都在 1273 行之前的注意力 kernel 里，见 [flash_fwd_kernel.h:974](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L974) 附近）。MMA 定义只是从解码 traits 复制骨架时留下的"器官"。

#### 4.2.4 代码实践：手推两组配置的全部常量

1. **实践目标**：对两组真实存在的配置，徒手展开 traits 的常量推导，确认你真正理解每个常量的含义。
2. **操作步骤**：对配置 A `(num_bits=4, group_size=128, quant_mode=1, kBlockN=128)` 与配置 B `(num_bits=2, group_size=32, quant_mode=1, kBlockN=256)`（kBlockN 由 `num_bits` 决定），逐项计算下表常量。
3. **需要观察的现象 / 预期结果**（答案）：

| 常量 | 公式 | A (4bit, g=128) | B (2bit, g=32) |
| --- | --- | --- | --- |
| `pack_num` | 16/num_bits | 4 | 8 |
| `kBlockN` | num_bits==4?128:256 | 128 | 256 |
| `kBlockP` | kBlockN/pack_num | 32 | 32 |
| `kBlockK_params` | kBlockN/group_size | 1 | 8 |
| `kHeadDim_pack` | 128/pack_num | 32 | 16 |
| `kHeadDim_k` | k-channel→128 | 128 | 128 |
| `kHeadDim_k_params` | k-channel→128 | 128 | 128 |
| `kHeadDim_v_params` | 128/group_size | 1 | 4 |
| `num_params` | kBlockN_pack/group_size | 128/128=1 | 256/32=8 |
| `tile_paramsk_j` | kBlockN/group_size | 1 | 8 |
| `tile_paramsk_k` | kHeadDim/16 | 8 | 8 |
| `tile_paramsk_g` | kBlockN/32 × (kBlockN/group_size) | 4×1=4 | 8×8=64 |
| `tile_paramsv_k` | kBlockN/16 | 8 | 16 |
| `kNThreads` | kNWarps(4)×32 | 128 | 128 |
| `kBlockKSmem` | kHeadDim%64==0→64 | 64 | 64 |
| `kSwizzle` | kBlockKSmem==32?2:3 | 3 | 3 |

注意 `kBlockP` 在两种位宽下都等于 **32**——每个 tile 打包输出恒为 32 行 uint16，与 u2-l2 讲的"两种位宽打包 tile 同占 32 个 uint16 行"互为印证。

4. **验证方式**：把上表公式写成一段 `constexpr` C++ 程序打印（把 `kHeadDim=128` 代入即可，无需 GPU）。示例代码：

```cpp
// 示例代码：独立编译验证常量表（g++ -std=c++17 constexpr_check.cpp）
#include <cstdio>
template<int NB, int GS> struct Q {
    static constexpr int pack_num = 16 / (NB == 128 ? 4 : 2);   // 借 kBlockN 区分位宽仅为示意
};
int main() {
    printf("A: kBlockP=%d num_params=%d\n", 128 / Q<128,128>::pack_num, 128/128);
    printf("B: kBlockP=%d num_params=%d\n", 256 / Q<256,32>::pack_num, 256/32);
}
```

输出应为 `A: kBlockP=32 num_params=1`、`B: kBlockP=32 num_params=8`。

#### 4.2.5 小练习与答案

**练习 1**：qpack traits 里 `SmemLayoutKPack` 为什么不需要 Swizzle，而解码 traits 需要？
**答案**：Swizzle 的目的是让 warp 用 LDSM（`ldmatrix`）从 smem 取矩阵碎片时不撞 bank conflict，这发生在"smem → 寄存器喂 MMA"的读路径上。qpack kernel 对打包 smem 只有寄存器→smem→gmem 的顺序写（`R2SCopyAtomPack` 是 `DefaultCopy`），没有碎片化读取，朴素行主序即可。

**练习 2**：`num_params = kBlockN_pack / group_size` 在配置 `(2bit, g=32)` 下等于 8，这个数字的物理含义是什么？
**答案**：一个 tile（256 个 token 行）内按 32 个 token 一组共切出 8 个量化分组，每组共享一对 scale/zero。它同时决定了寄存器里 `tScales/tZeros` 张量的第一维长度（[flash_fwd_kernel.h:1451-1453](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1451-L1453)）。

### 4.3 `run_flash_qpack`：grid 推导、共享内存与 `cudaFuncSetAttribute`

#### 4.3.1 概念说明

本模块是 host 侧最后一跳：拿 `params` 与 traits 常量算出 grid、block、smem 三要素并点火。qpack kernel 的并行策略非常直白——**一个 block 负责一个（序列、头、token 块）三元组**：每个 block 处理某条序列某个 head 上连续 `kBlockN` 个 token 的 K 和 V，把它们量化打包后写出。这天然适合三维 grid `(num_n_block, b, h)`。对比解码 kernel 的 grid（`(num_m_block, num_splits-1, b*h)`，见 [launch_template.h:85-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L85-L86)）：解码有 split-KV 切分维度，打包没有——打包是"恰好一次"的写任务，无需切分合并。

#### 4.3.2 核心流程

```text
run_kvcache_qpack_hdim128<T, quant_mode, num_bits, group_size>
  ├─ Headdim 固定 128；kBlockN = num_bits==4 ? 128 : 256
  ├─ 实例化 Flash_qpack_traits<128, kBlockN, 4, quant_mode, num_bits, group_size, T>
  ▼
run_flash_qpack<Kernel_traits>
  ├─ num_n_block = ⌈params.seqlen_k / kBlockN⌉
  ├─ grid = dim3(num_n_block, params.b, params.h)
  ├─ smem_size = Kernel_traits::kSmemSize
  ├─ if (smem_size >= 48KiB) cudaFuncSetAttribute(kernel, MaxDynamicSharedMemorySize, smem_size)
  └─ flash_qpack_kernel<<<grid, kNThreads, smem_size, stream>>>(params)
```

#### 4.3.3 源码精读

**从 dispatch 到 traits 实例化。** [csrc/bit_decode/src/flash_fwd_launch_template.h:200-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L200-L206)：

```cpp
template<typename T, int quant_mode, int num_bits, int group_size>
void run_kvcache_qpack_hdim128(Flash_fwd_params &params, cudaStream_t stream) {
    constexpr static int Headdim = 128;
    constexpr static int kBlockN = num_bits == 4 ? 128 : 256;
    run_flash_qpack<Flash_qpack_traits<Headdim, kBlockN, 4, quant_mode, num_bits, group_size, T>>(params, stream);
}
```

`kNWarps` 实参固定为 4（即 128 线程），`kBlockN` 随位宽切换：4-bit 一个 block 处理 128 个 token，2-bit 处理 256 个——两者打包输出都是 32 行 uint16（见 4.2.4 表），共享内存中打包区大小一致。

**启动本体。** [csrc/bit_decode/src/flash_fwd_launch_template.h:180-198](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L180-L198)：

```cpp
template<typename Kernel_traits>
void run_flash_qpack(Flash_fwd_params &params, cudaStream_t stream) {
    constexpr size_t smem_size = Kernel_traits::kSmemSize;
    const int num_n_block = (params.seqlen_k + Kernel_traits::kBlockN - 1) / Kernel_traits::kBlockN;
    dim3 grid(num_n_block, params.b, params.h);
    auto kernel = &flash_qpack_kernel<Kernel_traits>;
    if (smem_size >= 48 * 1024) {
        C10_CUDA_CHECK(cudaFuncSetAttribute(
            kernel, cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size));
    }
    kernel<<<grid, Kernel_traits::kNThreads, smem_size, stream>>>(params);
    C10_CUDA_KERNEL_LAUNCH_CHECK();
}
```

五行核心逻辑逐行读：

1. `num_n_block`：序列维 block 数，向上取整 \(\lceil \text{seqlen\_k} / \text{kBlockN} \rceil\)（尾块不足时 kernel 内有谓词保护）；
2. `grid(num_n_block, params.b, params.h)`：`params.b` 是从 `cu_seqlens_k` 恢复的 batch，`params.h` 是 `K_unpad.size(1)` 即 KV 头数（qpack 路径 `h == h_k`，`h_h_k_ratio=1`）；
3. `smem_size >= 48*1024` 判断：本 kernel **恒为真**（下节计算），opt-in 不可省；
4. 启动后 `C10_CUDA_KERNEL_LAUNCH_CHECK()` 捕获启动期错误（如 smem 超限、grid 越界）。

**kernel 定义宏。** [csrc/bit_decode/src/flash_fwd_launch_template.h:29-39](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L29-L39)：

```cpp
#define DEFINE_FLASH_QPACK_KERNEL(kernelName, ...) \
template<typename Kernel_traits> \
__global__ void kernelName(KERNEL_PARAM_MODIFIER const Flash_fwd_params params)

DEFINE_FLASH_QPACK_KERNEL(flash_qpack_kernel) {
    #if defined(ARCH_SUPPORTS_FLASH)
        flash::compute_qpack<Kernel_traits>(params);
    #else
        FLASH_UNSUPPORTED_ARCH
    #endif
}
```

与解码 kernel 的宏（带 9 个 bool 模板参数：Is_causal/Is_local/Split/...）相比，qpack 的宏**只有一个 Kernel_traits 参数**——它没有变长路径开关，行为完全由量化配置决定。`KERNEL_PARAM_MODIFIER` 即 `__grid_constant__`（sm_80+，[launch_template.h:14-19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L14-L19)），让按值传入的大结构体走只读常量内存路径（u3-l2 讲过）。

#### 4.3.4 代码实践：grid 与 SharedStorage 手算（本讲核心实践）

这就是本讲规格指定的实践任务。

1. **实践目标**：给定 `seqlen_k=1000, num_bits=4, b=2, h=32, d=128`，算出 grid 三维与 `SharedStorage` 字节数，解释 `cudaFuncSetAttribute` 的必要性。
2. **操作步骤**：
   - 第一步：定 `kBlockN`。4-bit → `kBlockN=128`。
   - 第二步：算 grid.x：`num_n_block = (1000 + 128 - 1) / 128 = 1127 / 128 = 8`（整除向下取整，8×128=1024 ≥ 1000，尾块只有 1000−7×128=104 个有效 token）。
   - 第三步：拼 grid 与 block：`grid = dim3(8, 2, 32)`，`block = kNThreads = 128`。
   - 第四步：按 4.2.3 的 `SharedStorage` 五个数组逐个算字节数（`kBlockP=32, kHeadDim_k=128, kHeadDim_pack=32`）。
3. **需要观察的现象 / 预期结果**（完整答案）：

| 数组 | 元素类型 | 元素数 | 字节 |
| --- | --- | --- | --- |
| `smem_K` | half (2B) | kBlockN×kHeadDim = 128×128 | 32768 |
| `smem_V` | half (2B) | 128×128 | 32768 |
| `smem_Kpack` | uint16 (2B) | kBlockP×kHeadDim_k = 32×128 | 8192 |
| `smem_Vpack` | uint16 (2B) | kBlockN×kHeadDim_pack = 128×32 | 8192 |
| `smem_reduce_tmp` | half (2B) | 32×32 | 2048 |
| **合计** | | | **83968 B = 82 KiB** |

（`cosize_v` 即各布局的元素总数；`array_aligned` 的对齐填充在这些人整尺寸下可忽略，结果按元素数×元素大小相加。）

**结论**：

- grid = **(8, 2, 32)**，共 8×2×32 = **512 个 block**，每 block 128 线程；
- `smem_size = 83968 B ≈ 82 KiB ≥ 48 KiB`，所以 [launch_template.h:189-192](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L189-L192) 的 `cudaFuncSetAttribute` **必然执行**——不把每 block 动态共享内存上限从 48 KiB 抬高，启动会直接失败。

4. **延伸计算（2-bit）**：`kBlockN=256, pack_num=8, kBlockP=32, kHeadDim_pack=16` 时：`smem_K = smem_V = 256×128×2 = 65536 B`，`smem_Kpack = 32×128×2 = 8192 B`，`smem_Vpack = 256×16×2 = 8192 B`，`reduce_tmp = 2048 B`，合计 **149504 B = 146 KiB**。注意它已逼近 sm_80 每 block 163 KiB 的 opt-in 上限；而 sm_86/89（RTX 30/40 系）每 block 上限只有 99 KiB，**2-bit qpack 配置在这些架构上即使抬高属性也会超限**——这从机制上解释了项目为何写死 sm_80/sm_90（此推断待本地验证；README 4090 图大概率对应 4-bit 配置，其 82 KiB 恰好在 99 KiB 之内）。
5. 有 GPU 的读者可用 `evaluation/test.py` 中的 4-bit 配置运行，配合 `nsys`/`ncu` 观察 `flash_qpack_kernel` 的 grid 与 shared memory 配置是否与手算一致（kernel 名会带模板哈希后缀）。

#### 4.3.5 小练习与答案

**练习 1**：`seqlen_k=1000, num_bits=2, b=2, h=32` 时 grid 是多少？
**答案**：2-bit → `kBlockN=256`，`num_n_block = ⌈1000/256⌉ = 4`（4×256=1024），grid = dim3(4, 2, 32) = 256 个 block。同一序列 2-bit 的 block 数只有 4-bit（8 个）的一半，但每 block 共享内存几乎翻倍（146 vs 82 KiB）。

**练习 2**：为什么 `run_flash_qpack` 里 `smem_size >= 48 * 1024` 的判断不能删掉（反正恒为真）？
**答案**：这是对 traits 的防御式解耦。`run_flash_qpack` 是通用模板，若未来某组 traits（如更小的 head_dim 或更小的 kBlockN）把 `kSmemSize` 压到 48 KiB 以下，判断会自动跳过 opt-in 调用，保持正确。写死"恒真"会在低配 traits 上浪费一次多余的 CUDA API 调用，但删掉判断则在高配 traits 上必然启动失败。

**练习 3**：`params.seqlen_k` 用的是 Python 传入的 `seqlen_k`（每序列长度），而不是 `k.size(1)`，为什么？
**答案**：非 paged 时 `k` 是折叠后的 `(b*s, h, d)`，`k.size(1)` 是头数 `h` 而非序列长度（[decode_api.cpp:636-637](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L636-L644) 里那些局部变量实际未被后续使用）；真正写进 `params.seqlen_k` 的是 `set_params_fprop_qpack` 收到的 `max_seqlen_k` 实参（[decode_api.cpp:659-661](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L659-L661)），grid 必须基于它计算。

### 4.4 `compute_qpack`：从 blockIdx 到全局内存偏移，kernel 内部的主流程

#### 4.4.1 概念说明

kernel 被调度后，每个 block 要回答三个问题：我是哪条序列（`bidb`）、哪个头（`bidh`）、哪段 token（`blockN_idx`）？然后用 u3-l2 讲过的 stride 字段把三元组换算成 8 个全局张量（FP16 K/V 输入 + 打包/参数输出）的元素偏移。这一步是"stride 非对称提取"设计的直接消费者。之后的主循环是干净的五段式：cp.async 装载 → 寄存器量化 → 参数直写 gmem → 打包结果经 smem 中转写 gmem。量化数学（`qpack_Kchannel_Vtensor` 内部）属于下一讲，本模块只看"骨架在哪里、数据怎么流"。

#### 4.4.2 核心流程

```text
compute_qpack:
  blockN_idx = blockIdx.x    （序列维 tile 编号）
  bidb       = blockIdx.y    （batch 编号）
  bidh       = blockIdx.z    （head 编号）
  └─ compute_qpack_1rowblock(params, bidb, bidh, blockN_idx):
       ① 算 8 组 row_offset（FP16 K/V、K/V pack、K/V params，均以元素计）
       ② cp.async 装载 K tile → smem_K；装载 V tile → smem_V（gmem_tiled_copy_QKV）
       ③ smem → 寄存器（tSsK → tSrK）
       ④ 量化 K：quant_mode==1 → qpack_Kchannel_Vtensor；否则 quant_Ktensor
       ⑤ scale/zero 从寄存器直接标量写 gK_params/gV_params
       ⑥ 量化 V（恒走 qpack_Kchannel_Vtensor）
       ⑦ 打包结果 寄存器 → smem（DefaultCopy）→ gmem（k_pack / v_pack）
```

#### 4.4.3 源码精读

**blockIdx 映射。** [csrc/bit_decode/src/flash_fwd_kernel.h:1636-1646](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1636-L1646)：

```cpp
template<typename Kernel_traits, typename Params>
inline __device__ void compute_qpack(const Params &params) {
    const int blockN_idx = blockIdx.x;
    const int bidb = blockIdx.y;
    const int bidh = blockIdx.z;
    flash::compute_qpack_1rowblock<Kernel_traits>(params, bidb, bidh, blockN_idx);
}
```

三个维度与 4.3 的 `dim3 grid(num_n_block, params.b, params.h)` 一一对应。注意解码 kernel 的 `compute_attn`（[flash_fwd_kernel.h:1650 以下](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1650-L1654)）是同样的三行开头——这是 FlashAttention 家族的标准开场。

**偏移计算。** [csrc/bit_decode/src/flash_fwd_kernel.h:1312-1321](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1312-L1321)：

```cpp
const index_t row_offset_k = block_table == nullptr
    ? binfo.k_offset(params.k_batch_stride, params.k_row_stride, bidb_cache)
      + blockN_idx * kBlockN * params.k_row_stride + (bidh / params.h_h_k_ratio) * params.k_head_stride
    : ...;
const index_t row_offset_k_pack = block_table == nullptr
    ?  binfo.k_offset(params.K_pack_batch_stride, params.K_pack_row_stride, bidb_cache)
      + blockN_idx * kBlockP * params.K_pack_row_stride + (bidh / params.h_h_k_ratio) * params.K_pack_head_stride
    : ...;
```

通用公式（k-channel、非 paged）：

\[ \text{offset} = \text{bidb} \times \text{batch\_stride} + \text{tile} \times \text{tile\_rows} \times \text{row\_stride} + \text{bidb}_{h} \times \text{head\_stride} \]

其中 FP16 K 用 `kBlockN` 行/tile，打包 K 用 `kBlockP` 行/tile（缩小了 `pack_num` 倍）。`binfo.k_offset` 里 `cu_seqlens_k` 传的是 `nullptr`（[decode_api.cpp:666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L666)），因此 `sum_s_k == -1`，退化为 `bidb * batch_stride`（[block_info.h:33-37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L33-L37)），`batch_stride` 正是 4.1.3 里手工重算的 `seqlen_k * h * d`。V 系偏移完全镜像（[flash_fwd_kernel.h:1323-1332](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1323-L1332)）。

**装载与量化主流程。** [csrc/bit_decode/src/flash_fwd_kernel.h:1437-1477](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1437-L1477)：

```cpp
cute::copy(gmem_tiled_copy_QKV, tKgK, tKsK);      // K: gmem → smem（cp.async，uint128 一次 8 个 half）
cute::cp_async_fence();
flash::cp_async_wait<0>();
__syncthreads();
cute::copy(gmem_tiled_copy_QKV, tVgV, tVsV);      // V: gmem → smem
cute::cp_async_fence();
cute::copy(smem_tiled_copy_K, tSsK, tSrK_view);   // K: smem → 寄存器

if (Kernel_traits::quant_mode == 1) {
    quant::qpack_Kchannel_Vtensor<num_bits>(tSrK, tSrK_pack, tScales_k_c, tZeros_k_c, sReduce_tmp, num_params);
} else {
    quant::quant_Ktensor(tSrK, tSrK_pack, tScales_k_g, tZeros_k_g, num_params);
}
...
quant::qpack_Kchannel_Vtensor<num_bits>(tSrV, tSrV_pack, tScales_v_c, tZeros_v_c, sReduce_tmp, num_params);
```

可以看到 K/V 装载是重叠的：V 的 cp.async 发出后立刻做 K 的 smem→寄存器拷贝与量化，等要用 V 时才 `cp_async_wait` + `__syncthreads`（[flash_fwd_kernel.h:1473-1475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1473-L1475)）——典型的双缓冲式 latency 隐藏（ albeit 手工排布）。

**参数与打包结果的两种写出方式。** scale/zero 走标量循环直接写 gmem（[flash_fwd_kernel.h:1489-1497](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1489-L1497) 的 `gK_params(...) = tScales_k_h2_c(j, i);`）；打包数据则经 smem 中转用 tile 拷贝写出（[flash_fwd_kernel.h:1506-1521](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1506-L1521)）：

```cpp
cute::copy(smem_tiled_copy_kv_pack, tSrK_pack_r2s_view, tSsK_pack_r2s);  // 寄存器 → smem
__syncthreads();
cute::copy(gmem_tiled_copy_k_pack, tKsK_pack_s2g, tKgK_pack_s2g);        // smem → gmem (k_pack)
cute::copy(gmem_tiled_copy_v_pack, tVsV_pack_s2g, tVgV_pack_s2g);        // smem → gmem (v_pack)
```

注意 2-bit 的 V 打包因 `kHeadDim_pack=16` 只有半行宽，代码在 [flash_fwd_kernel.h:1509-1515](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1509-L1515) 专门只让前 64 个线程参与写出（`tidx < 64`），对应 traits 里 `GmemTileCopyV_Pack` 的 `(64,2)` 线程布局（[kernel_traits.h:570-573](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L570-L573)）——常量表与 kernel 行为在这里对上了号。

#### 4.4.4 代码实践：跟踪一个 block 的偏移计算（源码阅读型实践）

1. **实践目标**：对指定的 block 坐标手工算出它读写的全局偏移，验证你理解 grid↔数据的映射。
2. **操作步骤**：取 `seqlen_k=1024`（取可整除值便于手算；grid 计算见 4.3.4，此处 `num_n_block=8`）、`b=2, h=32, d=128, num_bits=4, group_size=128, quant_mode="k-channel"`，跟踪 block `(blockN_idx=5, bidb=1, bidh=3)`。所有张量按 contiguous 布局算 stride（元素计数），代入 4.4.3 的公式。
3. **需要观察的现象 / 预期结果**（答案）：
   - FP16 K（折叠后 `(2048, 32, 128)`）：`k_batch_stride = 1024×32×128 = 4194304`，`k_row_stride = 32×128 = 4096`，`k_head_stride = 128`。
     `row_offset_k = 1×4194304 + 5×128×4096 + 3×128 = 4194304 + 2621440 + 384 = 6816128`（half 元素）。
   - k_pack（`(2, 256, 32, 128)` uint16）：batch/row/head stride = `1048576 / 4096 / 128`。
     `row_offset_k_pack = 1×1048576 + 5×32×4096 + 3×128 = 1048576 + 655360 + 384 = 1704320`（uint16 元素）。
   - k_params（`(2, 8, 32, 128)`）：`kBlockK_params = 128/128 = 1` 行/tile，batch/row/head stride = `32768 / 4096 / 128`。
     `row_offset_k_params = 1×32768 + 5×1×4096 + 3×128 = 32768 + 20480 + 384 = 53632`。
   - 核对：k_pack 偏移恰为 FP16 K 偏移的 1/4（pack_num=4），k_params 偏移为其 1/128（group_size=128）——低比特压缩比直接体现在同一 block 三个张量的偏移比例上。
4. **验证方式**：上述为纯静态推导，可对照 [flash_fwd_kernel.h:1312-1332](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1312-L1332) 逐项核对；若想动态验证，可在本地 fork 中于偏移计算后加一行 `printf`（属于对源码的临时修改，验证后还原），用小配置跑一次比对输出（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：qpack kernel 中 `params.h_h_k_ratio` 一定是 1，为什么？
**答案**：qpack 的 `num_heads` 与 `num_heads_k` 都取自同一个 K 张量的尺寸（[decode_api.cpp:636-638](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L636-L638)），二者相等，所以偏移公式里 `(bidh / params.h_h_k_ratio)` 恒等于 `bidh`。解码路径才有 GQA（Q 头多于 KV 头）需要真正的比例。

**练习 2**：为什么打包结果要先写 smem 再写 gmem，而不直接寄存器→gmem？
**答案**：量化在寄存器里产出的是按线程碎片的 uint16 值，直接散写 gmem 会形成非合并访问；先按 `smem_tiled_copy_kv_pack` 归置到 smem 的规则 tile，再用 `gmem_tiled_copy_k_pack`（每次 8 个 uint16 = 16B 向量化store）合并写出。smem 在这里充当"访问模式整形器"。

**练习 3**：`flash_qpack_kernel` 的宏定义没有任何 `Is_causal`/`Split` 之类的 bool 模板参数，这对编译体积意味着什么？
**答案**：每个量化配置只实例化一个 kernel 变体；对比解码 kernel 每个配置要为多组 bool 组合生成变体（尽管 `run_flash_splitkv_fwd` 实际只用了固定组合），qpack 的实例化空间小得多——这也是 genfile 里 qpack 文件远小于 splitkv 文件的原因之一。

## 5. 综合实践：为一份配置制作完整的「启动卡片」

把本讲四个模块串起来。给定配置：`quant_mode="k-channel", num_bits=2, group_size=32, b=4, seqlen_k=2048, h=8 (KV 头), d=128`，请不运行代码，写出这张卡片（全部答案如下，先自己算再核对）：

| 卡片项 | 答案 | 依据 |
| --- | --- | --- |
| Python 调用 | `bit_decode.kvcache_pack_int(..., num_bits=2)` → `bit_decode_cuda.kvcache_pack_int2` | [bit_decode_interface.py:35-43](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L35-L43) |
| dispatch 命中 | `run_kvcache_qpack_<half_t, 128, 1, 2, 32>` | [decode_api.cpp:222-223](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L222-L223) |
| 实例化单元 | `flash_qpack_hdim128_fp16_sm80_2bit.cu` 的 `<1,2,32>` 特化 | [flash_qpack_hdim128_fp16_sm80_2bit.cu:15-18](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu#L15-L18) |
| traits 常量 | `kBlockN=256, kBlockP=32, kHeadDim_pack=16, num_params=8, kNThreads=128` | 4.2.4 表 B 列 |
| grid | `num_n_block=⌈2048/256⌉=8` → dim3(8, 4, 8) = 256 blocks | [launch_template.h:184-185](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L184-L185) |
| smem | 65536+65536+8192+8192+2048 = **149504 B (146 KiB)** | 4.3.4 延伸计算 |
| `cudaFuncSetAttribute` | **触发**（≥48 KiB），且要求 GPU ≥ sm_80 | [launch_template.h:189-192](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L189-L192) |
| 每 block 职责 | 序列 `blockIdx.y`、头 `blockIdx.z` 上连续 256 个 token 的 K+V 量化打包 | 4.4 |

有 GPU 的读者：把 `evaluation/test.py` 的参数改成上述配置（test.py 默认即有 2-bit 分支）跑一次，用 `ncu --set full`（或 `nsys`）抓 `flash_qpack_kernel`，核对 grid 与 shared memory；无 GPU 的读者完成卡片并逐项写出依据行号即算通过。

## 6. 本讲小结

- **启动链五环**：Python 折叠 batch 并按 `num_bits` 分流 → `kvcache_qpack` host 包装（恢复 batch、填参数）→ `run_kvcache_qpack` dispatch if 链 → genfile 显式模板实例化 → `run_flash_qpack` 点火；dispatch 分支、genfile 实例、setup.py 源列表三者必须成对存在。
- **`Flash_qpack_traits` 是解码 traits 的简化版**：无 Q/acc 共享内存、无 Swizzle 打包布局、无 MMA 调用；`SharedStorage` 只有 FP16 K/V 输入暂存、打包输出暂存与归约暂存 5 个数组。
- **grid 三维有明确语义**：`(⌈seqlen_k/kBlockN⌉, b, h)`，`kBlockN` 随位宽取 128（4-bit）/256（2-bit），两种位宽的打包输出 tile 恒为 32 行 uint16。
- **共享内存恒超 48 KiB**：4-bit 约 82 KiB、2-bit 约 146 KiB，因此 `cudaFuncSetAttribute` 的 opt-in 必不可少；2-bit 的大 smem 也隐含了"仅 sm_80/sm_90 可跑"的架构约束。
- **kernel 内部**：blockIdx 三元组经 `row_offset` 公式换算成 8 个张量的元素偏移（k_pack 偏移是 FP16 K 的 1/pack_num，k_params 是 1/group_size），主流程为 cp.async 装载 → 寄存器量化 → 参数标量直写 + 打包数据经 smem 合并写出。

## 7. 下一步学习建议

本讲只走到了 `quant::qpack_Kchannel_Vtensor` 的门口。下一讲 **u4-l2（归约原语：warp_reduce、allreduce_ 与分组 max/min）** 将打开 `csrc/bit_decode/src/include/qpack.h`，看 `sReduce_tmp` 这块 32×32 共享内存如何配合 warp 洗牌指令算出每个量化组的 max/min；随后 **u4-l3（量化与打包落盘）** 解释 scale/zero 公式与 `dequantize.h` 的互逆关系。建议在继续之前，先重读本讲的 4.2.4 常量表——后面两讲的所有寄存器张量形状（`tScales/tZeros` 的 `4*num_params` 维等）都由这张表决定。若你想先看"打包结果如何被消费"，可以跳到第五单元的 u5-l1（解码 kernel traits）对照两种 traits 的 `SmemLayoutKPack` 差异。
