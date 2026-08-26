# pybind11 入口:mha_fwd_kvcache 与 kvcache_qpack 的参数校验与分发

## 1. 本讲目标

上一讲(u2-l3)我们止步于 `bit_decode_cuda` 这个"黑盒模块":Python 侧把 `num_bits` 分流成 `int2`/`int4` 两个绑定函数名,然后就把参数一股脑扔了进去。本讲就打开这个黑盒,读完 `decode_api.cpp`——从 Python 函数调用落到 GPU kernel 启动之间全部的 C++ 代码。学完本讲,你应该能够:

1. 说出 `PYBIND11_MODULE` 导出的 4 个函数,以及 C++ 模板参数 `num_bits` 在这条链上的作用;
2. 完整追踪 `fwd_kvcache_int4` 从 Python 调用到 `mha_fwd_kvcache<4>`、再到三个 GPU kernel 启动的每一步;
3. 理解 `CHECK_DEVICE`/`CHECK_SHAPE`/`CHECK_CONTIGUOUS` 等防御性校验宏的行为,以及当前仓库的一个著名陷阱——传入未启用的 `quant_mode`/`group_size` 组合会**静默无操作**而不是报错。

本讲不深入 `Flash_fwd_params` 结构体的字段细节(下一讲 u3-l2)和 `num_splits_heuristic` 的算法(u3-l3),只把它们当作调用链上的"站点"标注出来。

## 2. 前置知识

**pybind11 与 torch 扩展模块。** PyTorch 允许用 C++/CUDA 编写扩展,编译成一个可直接 `import` 的 Python 模块(本项目里叫 `bit_decode_cuda`)。pybind11 是连接层:一行 `m.def("函数名", &C++函数指针, "说明")` 就把 C++ 函数注册成 Python 可调用的函数,参数和返回值会自动做 Python 对象 ↔ C++ 类型(如 `at::Tensor`、`std::string`、`std::tuple`)的转换。转换失败时 Python 侧会抛 `TypeError`。

**`c10::optional<T>`。** PyTorch 版的 `std::optional`。绑定到 Python 后对应 `None` 或一个张量。本讲会看到大量 `c10::optional<const at::Tensor> &k_` 形式的参数——它允许"这个输入可以不传",代码里用 `k_.has_value()` 判断。

**编译期模板参数 vs 运行时参数(承接 u2-l3)。** CUDA kernel 的性能高度依赖编译期常量(块大小、打包粒度等),所以 `quant_mode`、`num_bits`、`group_size` 这些"配置"在 kernel 层是 C++ 模板参数,在编译时就固化了。但 Python 传进来的是普通运行时值(字符串、整数)。于是在 C++ 层必须有一段 `if (params.quant_mode == "k-channel") { if (params.group_size == 128) { 调用模板实例 A } }` 式的**分发代码**,把运行时值映射到某个编译好的模板实例上。本讲的 `run_mha_fwd` / `run_kvcache_qpack` 就是这段分发代码。

**`TORCH_CHECK` 与防御性校验。** `TORCH_CHECK(条件, 消息)` 是 PyTorch 的断言宏:条件为假时抛出带消息的 C++ 异常,Python 侧表现为 `RuntimeError`。注意它在**宿主端(CPU 侧)同步执行**,检查的是元信息(形状、dtype、stride、设备),代价极低,却能把"传错参数"的错误拦在 kernel 启动之前,而不是让 GPU 上出现难以调试的越界或错误结果。

**GQA(分组查询注意力)。** decode 阶段的 Query 头数 `num_heads` 通常多于 KV 头数 `num_heads_k`(例如 32 个 Q 头共享 8 个 KV 头),`ngroups = num_heads / num_heads_k`。这给了 kernel 一种优化空间——本讲会看到著名的 `seqlenq_ngroups_swapped` 重排技巧。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [csrc/bit_decode/decode_api.cpp](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp) | **本讲主角**:pybind11 绑定 + 参数校验 + 模板分发 | 全文约 700 行,全部读完 |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py) | Python 侧入口,u2-l3 已精读 | 只看它传给绑定函数的参数顺序 |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h) | dispatch 终点:真正启动 GPU kernel 的地方 | 只看 `run_mha_fwd_splitkv_dispatch` / `run_flash_qpack`,知道链路通到这里即可 |
| [csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu) | 模板显式实例化清单(哪些配置真的被编译了) | dispatch 表的"另一半证据" |
| [csrc/bit_decode/src/include/flash.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h) | 三个模板函数的声明 | 第 203-205 行 |
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | kernel 正确性测试 | 只看 `cu_seqlens_k` 的构造 |

## 4. 核心概念与源码讲解

### 4.1 PYBIND11_MODULE:四个导出函数与 num_bits 模板分流

#### 4.1.1 概念说明

整个 CUDA 扩展对 Python 暴露的接口只有 4 个函数,全部集中在文件末尾的 `PYBIND11_MODULE` 宏里。它们是两两配对的:`kvcache_pack_int2/4` 负责 prefill 阶段的量化打包,`fwd_kvcache_int2/4` 负责 decode 阶段的低比特注意力。数字后缀 2/4 就是 `num_bits`。

关键在于:这 4 个绑定并不是 4 个独立写好的函数,而是**同一个 C++ 函数模板的 4 个实例**——`kvcache_qpack<2>`、`kvcache_qpack<4>`、`mha_fwd_kvcache<2>`、`mha_fwd_kvcache<4>`。`num_bits` 作为模板参数 `template<int num_bits>` 进入编译期,所以每个实例内部的 kernel 调用链都带着固化的位宽。这也解释了 u2-l3 看到的现象:Python 层必须在 `num_bits == 4` / `num_bits == 2` 两个分支里写两次几乎相同的调用——因为编译期参数没法在运行时"晚绑定",只能在 Python 层先分流。

#### 4.1.2 核心流程

```text
Python 调用 kvcache_pack_int(..., num_bits=4)
        │  num_bits 是普通 int,运行时值
        ▼
bit_decode_interface.py: if num_bits == 4 → bit_decode_cuda.kvcache_pack_int4(...)
        │  函数名已经携带了位宽信息
        ▼
pybind11 查表:m.def("kvcache_pack_int4", &kvcache_qpack<4>)
        │  绑定到模板实例 kvcache_qpack<4>,num_bits 从此固化
        ▼
C++ 函数体内继续把 num_bits 传给下一层模板 run_kvcache_qpack<4>
```

#### 4.1.3 源码精读

导出注册表(注意 `m.doc()` 说明整个模块就叫 "BitDecoding"):

[decode_api.cpp:688-694](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L688-L694) — `PYBIND11_MODULE` 把 4 个模板实例注册为 Python 函数;`&kvcache_qpack<2>` 这种写法是"取模板实例的函数指针"。

```cpp
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.doc() = "BitDecoding";
    m.def("kvcache_pack_int2", &kvcache_qpack<2>, "...(2-bit)");
    m.def("kvcache_pack_int4", &kvcache_qpack<4>, "...(4-bit)");
    m.def("fwd_kvcache_int2",  &mha_fwd_kvcache<2>, "...2-bit KV-cache");
    m.def("fwd_kvcache_int4",  &mha_fwd_kvcache<4>, "...4-bit KV-cache");
}
```

`TORCH_EXTENSION_NAME` 是编译时由 setup.py 注入的宏,值为 `bit_decode_cuda`(见 [setup.py:128](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L128) 的 `name="bit_decode_cuda"`),这决定了 Python 侧 `import bit_decode_cuda` 的模块名。

Python 侧的分流逻辑(u2-l3 已读,这里只看传参顺序的对应关系):

[bit_decode_interface.py:63-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L63-L82) — `fwd_kvcache_int` 的 `num_bits==4` 分支,按位置传参调用 `fwd_kvcache_int4`。

```python
if num_bits == 4:
    out_bit, k_pack_new, ... = bit_decode_cuda.fwd_kvcache_int4(
        q,
        k_pack, k_params,
        v_pack, v_params,
        opt_k_new, opt_v_new, opt_seqlens_k,
        k_pack_new, k_params_new, v_pack_new, v_params_new,
        opt_block_table,
        softmax_scale,
        quant_mode, group_size, residual_block_size, new_lens,
        False,   # is_causal
        -1, -1,  # window_size_left / right
        0.0,     # softcap
        True,    # is_rotary_interleaved
        0        # num_splits
    )
```

pybind11 默认按**位置**匹配参数(除非 C++ 侧用 `py::arg` 声明名字,本项目没用),所以这 24 个实参必须与下一节 `mha_fwd_kvcache` 的 C++ 签名严格一一对齐——多传、少传、错位都会在 Python 层直接抛 `TypeError`。末尾 6 个写死的 `# Added` 参数(is_causal、窗口左右、softcap、rotary 标志、num_splits)说明本项目永远以"非因果、无窗口、无 softcap、自动 split"的配置调用,把原版 FlashAttention 的通用性收窄成 decode 专用的窄接口。

#### 4.1.4 代码实践

**实践目标**:亲手"看见"pybind11 注册出来的函数签名,把 Python 参数与 C++ 签名对上。

**操作步骤**:

1. 在已按 u1-l2 编译安装好的环境中运行:

```bash
python -c "
import bit_decode_cuda, inspect
print([n for n in dir(bit_decode_cuda) if not n.startswith('_')])
help(bit_decode_cuda.fwd_kvcache_int4)
"
```

2. 把 help 打印出的参数列表,与 [decode_api.cpp:317-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L317-L341) 的 C++ 签名逐个位置对照,数一数是否恰好 24 个。
3. 再故意少传一个参数(例如去掉末尾的 `0`),观察 `TypeError` 报错中列出的期望参数个数。

**需要观察的现象**:help 输出里每个参数的类型标注(`at::Tensor`、`float`、`str`、`int`、`bool`);`c10::optional<...>` 会显示为 `Optional[...]`,对应 Python 的 `None`。

**预期结果**:`dir()` 列出且仅列出 4 个函数;参数个数与 C++ 签名一致;少传参数报 `TypeError`。

**待本地验证**:本实践需要已编译的 `bit_decode_cuda`(需 GPU 工具链)。无 GPU 环境下可改为纯源码对照:把 [bit_decode_interface.py:64-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L64-L82) 的 26 个实参抄成一列,在旁边写上 C++ 签名里的形参名,做成对照表。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `num_bits` 不做成 `m.def` 的一个普通 `int` 参数,而是拆成 4 个函数?

**答案**:因为 `num_bits` 在 kernel 层是 C++ 模板参数(`template<int num_bits>`),必须在编译期确定。pybind11 无法把运行时传入的 int 转成模板参数,所以只能在 Python 层分流到不同函数名,每个名字绑定一个已编译好的模板实例。

**练习 2**:如果调用 `bit_decode_cuda.fwd_kvcache_int4` 时把 `quant_mode` 和 `group_size` 的位置传反了(先传 int 再传 str),会发生什么?

**答案**:pybind11 的类型转换失败,Python 侧抛 `TypeError`(试图把 `str` 转成 `int` 或反之),不会进入 C++ 函数体。这类错误在函数体执行前就被拦截,这正是静态绑定相较纯 Python 的额外安全保障。

---

### 4.2 mha_fwd_kvcache<num_bits>:decode 主入口的校验、GQA 重排与强制 split

#### 4.2.1 概念说明

`mha_fwd_kvcache` 是每轮 decode 的主入口,也是 `decode_api.cpp` 中最长的函数(约 210 行)。它承接 Python 传来的 26 个参数,做四件事:

1. **防御性校验**:设备、dtype、形状、stride 逐项检查,把错误拦在 kernel 启动前;
2. **GQA 重排**:`seqlenq_ngroups_swapped`——当 Q 只有 1 个 token、且 Q 头数多于 KV 头数时,把"头维度"折叠进"序列维度",让 kernel 用更大的并行度跑;
3. **组装参数结构体** `Flash_fwd_params`(经 `set_params_fprop` 与 `set_params_splitkv` 两个填表函数,细节留给 u3-l2/u3-l3);
4. **强制走 split kernel** 并返回 5 元组。

#### 4.2.2 核心流程

```text
mha_fwd_kvcache<4>(q, k_pack, k_params, v_pack, v_params, k_, v_, seqlens_k_,
                   k_pack_new×4, block_table_, softmax_scale, quant_mode,
                   group_size, residual_block_size, new_lens, ...)
  ├─ ① GPU 代际检查(SM80/SM90)
  ├─ ② CHECK_DEVICE(q);paged_KV 时校验 block_table
  ├─ ③ 从 q / k_pack / v_pack 的 sizes 提取 b、seqlen_q、h、d、seqlen_k、h_k
  ├─ ④ seqlen_q==1 → is_causal 强制 false
  ├─ ⑤ GQA: seqlenq_ngroups_swapped 判定 + q 重排
  ├─ ⑥ CHECK_SHAPE(q, ...);分配 out、softmax_lse
  ├─ ⑦ set_params_fprop:把指针/stride/尺度填进 params(读 q、k_pack、k_params、
  │     v_pack、v_params、out、softmax_lse)
  ├─ ⑧ 若 k_ 有值:校验 k/v/seqlens_k,填 params 的 knew/vnew/×_new 字段
  ├─ ⑨ set_params_splitkv:跑 num_splits 启发式,分配 float 累积缓冲
  ├─ ⑩ run_mha_fwd<4>(params, stream, force_split_kernel=true)
  │      └─(见 4.3)→ 三个 GPU kernel:residual → splitkv → combine
  └─ ⑪ GQA 逆重排 out;返回 (out, k_pack_new, k_params_new, v_pack_new, v_params_new)
```

#### 4.2.3 源码精读

**(a) 三个校验宏**。文件开头定义了本文件反复使用的三件套:

[decode_api.cpp:19-21](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L19-L21) — `CHECK_DEVICE` 查张量是否在 GPU 上;`CHECK_SHAPE` 用 `torch::IntArrayRef` 比较 sizes 向量;`CHECK_CONTIGUOUS` 查内存连续性。`#x` 是字符串化宏,让报错消息里带上变量名(如 `"q must be on CUDA"`)。

```cpp
#define CHECK_DEVICE(x)    TORCH_CHECK(x.is_cuda(), #x " must be on CUDA")
#define CHECK_SHAPE(x, ...) TORCH_CHECK(x.sizes() == torch::IntArrayRef({__VA_ARGS__}), ...)
#define CHECK_CONTIGUOUS(x) TORCH_CHECK(x.is_contiguous(), #x " must be contiguous")
```

**(b) 代际检查**。[decode_api.cpp:343-347](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L343-L347) — 通过 `at::cuda::getCurrentDeviceProperties()` 读取当前 GPU 的 compute capability,只放行 SM8.x(安培,A100/4090 等)与 SM9.0(Hopper H100),否则抛 `"FlashAttention only supports Ampere GPUs or newer."`。这与 u1-l2 讲过的 setup.py 里 `sm_80`/`sm_90` 编译目标一一对应。

**(c) 尺寸提取**。decode 时 Q 只有一个 token,所以 `seqlen_k` 等 KV 侧尺寸无法从 q 拿到,只能从打包缓存的形状反推:

[decode_api.cpp:363-378](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L363-L378) — 从 `q.sizes()` 取 batch/头数/头维;`num_heads_k` 取自 `k_pack.size(2)`;非 paged 时 `batch_size_c = k_pack.size(0)`。随后一串 `TORCH_CHECK` 约束:batch 为正、头维 ≤ 256、**Q 头数必须能被 KV 头数整除**(GQA 的硬约束)。

**(d) GQA 重排**——本函数最精巧的一段:

[decode_api.cpp:383-391](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L383-L391) — 满足"单 token Q + Q 头多于 KV 头 + 非滑动窗口 + 头维是 8 的倍数"时,把 q 从 `(b, 1, h, d)` 重塑-转置成 `(b, ngroups, h_k, d)`。

```cpp
const int seqlenq_ngroups_swapped = seqlen_q == 1 && num_heads > num_heads_k
                                    && window_size_left < 0 && window_size_right < 0
                                    && head_size_og % 8 == 0;
if (seqlenq_ngroups_swapped) {
    const int ngroups = num_heads / num_heads_k;
    q = q.reshape({batch_size, num_heads_k, ngroups, head_size_og}).transpose(1, 2);
    seqlen_q = ngroups;      // “序列长”变成了组数
    num_heads = num_heads_k; // 头数变成 KV 头数
}
```

直觉:kernel 的并行度来自 `(序列块 × 头数)`。GQA decode 下原本是 `1 × 32 = 32` 个查询行,重排后等效于 `4 × 8 = 32`——总量不变,但 kernel 内部按 16 行一个 tile 切分时,4 个"伪 token"能填满更多 tile,减少浪费。代价是最后要把输出变回去:

[decode_api.cpp:521-525](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L521-L525) — 逆重排 `out.transpose(1,2).reshape({batch_size, 1, num_heads_k * seqlen_q, head_size_og})`,把 `(b, ngroups, h_k, d)` 还原成 `(b, 1, h, d)`,再连同 4 个 `*_new` 一起打包成 tuple 返回。

**(e) 新 KV 分支的校验与填表**。`k_`/`v_` 是残余区(含新 token)的 FP16 张量,是可选参数:

[decode_api.cpp:441-460](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L441-L460) — 传了 k 就必须同时传 v 和 seqlens_k;检查 k/v 的 dtype 与 q 一致、最后一维连续(`stride(-1)==1`,kernel 按向量化内存访问的前提)、形状恰为 `(batch, seqlen_knew, h_k, d)`;seqlens_k 必须是 int32、连续、形状 `(batch,)`。

[decode_api.cpp:462-498](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L462-L498) — 校验通过后,把 `new_lens`、`seqlen_knew`、knew/vnew 的指针与三级 stride、以及 4 个 `*_new` 输出缓冲的指针和 stride 全部填进 `params`。注意第 475 行的 `const int pack_nums = 16 / num_bits;`——这个局部变量算出来后**并未被写入 params**(kernel 侧会用模板参数重新推导),是源码阅读时容易困惑的一处"残留代码"。

**(f) 强制 split**:

[decode_api.cpp:516-519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L516-L519) — 注释写明原因:只有 split kernel 支持向 KV cache 追加新 token、以及 paged KV。所以 `force_split_kernel=true` 是无条件的,`run_mha_fwd` 的非 split 分支在本项目中永远不会从这条链走进去。

```cpp
auto stream = at::cuda::getCurrentCUDAStream().stream();
// Only split kernel supports appending to KV cache, or indexing to the cache
// with cache_batch_idx, or paged KV cache
run_mha_fwd<num_bits>(params, stream, /*force_split_kernel=*/true);
```

**(g) 一个值得注意的"哑参数"**:C++ 签名里的 `residual_block_size`([decode_api.cpp:333](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L333))在 `mha_fwd_kvcache` 函数体内**没有被引用**(全文检索仅出现在签名处)。kernel 实际使用的块大小来自编译期常量 [kernel_traits.h:75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L75) 的 `residual_block_size = num_bits == 4 ? 128 : 256`。也就是说 Python 侧改传这个参数不会改变行为——这呼应 u2-l2 的结论:块大小由位宽决定,是编译期事实。

#### 4.2.4 代码实践

**实践目标**:画出 `fwd_kvcache_int4` 的完整调用时序图,并在每个阶段标注它读取的输入张量。(本讲的综合任务,也是检验你是否真正读懂本讲的手段。)

**操作步骤**:

1. 通读 [decode_api.cpp:315-526](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L315-L526),按 4.2.2 的流程骨架,给每个编号站点补上"读取了哪些张量、写了哪些张量";
2. 用纸笔或任意识别工具画成时序图/流程图;
3. 与下面的参考答案对照。

**参考答案(文字版时序图)**:

```text
Python: fwd_kvcache_int(q, k_pack, k_params, v_pack, v_params,
                        opt_k_new, opt_v_new, opt_seqlens_k,
                        k_pack_new×4, opt_block_table,
                        softmax_scale, quant_mode, group_size,
                        residual_block_size, new_lens, num_bits=4)
   读:全部 18 个逻辑输入(除分流用的 num_bits 外)
   │  num_bits==4 分流 (interface.py:63)
   ▼
pybind11: m.def("fwd_kvcache_int4", &mha_fwd_kvcache<4>)   (decode_api.cpp:692)
   │  类型转换: Tensor/Optional[Tensor]/float/str/int/bool → C++
   ▼
mha_fwd_kvcache<4>                                          (decode_api.cpp:315)
   ① 代际检查        读: dprops(设备属性,343-347)
   ② 设备/分页检查    读: q, block_table_(349-358)
   ③ 尺寸提取+约束    读: q.sizes(), k_pack.size(2), v_pack.size(1)(360-378)
   ④ GQA 判定与重排   读/写: q(383-391)
   ⑤ 形状校验+分配    读: q;写: out = empty_like(q), softmax_lse(393-413)
   ⑥ set_params_fprop 读: q,k_pack,k_params,v_pack,v_params,out,softmax_lse
   │                   写: params(415-438)
   ⑦ 新 KV 分支      读: k_,v_,seqlens_k_;校验后读 k_pack_new×4 的指针与 stride
   │                   写: params 的 knew/vnew/×_new 字段(440-500)
   ⑧ set_params_splitkv 读: b,h,d,seqlen_k,seqlen_q,dprops
   │                   写: params.num_splits;分配 softmax_lse_accum,out_accum
   │                   (512-514;算法在 u3-l3)
   ▼
run_mha_fwd<4>(params, stream, force_split_kernel=true)     (decode_api.cpp:183)
   │  quant_mode=="k-channel"?
   │    group_size==128 → run_mha_fwd_splitkv_dispatch<half,128,false,1,4,128>
   │    group_size==32  → run_mha_fwd_splitkv_dispatch<half,128,false,1,4,32>
   │    其余组合:无 kernel 启动(见 4.3)
   ▼
run_flash_splitkv_fwd(flash_fwd_launch_template.h:76)
   启动 kernel① flash_fwd_residual_kernel  grid=(m块, b, h)      (90-96)
   启动 kernel② flash_fwd_splitkv_kernel   grid=(m块, splits-1, b*h)(98-104)
   启动 kernel③ flash_fwd_splitkv_combine_kernel(splits>1 时)   (106-124)
   ▼
回到 mha_fwd_kvcache: GQA 逆重排 out(521-523)
   ▼
返回 (out, k_pack_new, k_params_new, v_pack_new, v_params_new)(525)
```

**需要观察的现象**:画完后自查两点——(1) `set_params_fprop` 只"读指针和 stride"不搬运数据,真正的数据移动全部发生在三个 GPU kernel 里;(2) `out` 与 4 个 `*_new` 是**调用方预分配**的,`mha_fwd_kvcache` 里没有为它们 `torch::empty`(除了内部缓冲 `softmax_lse` 和 split 累积缓冲)。

**预期结果**:你的图与参考答案的站点顺序、张量标注一致,即可认为本讲核心链路已掌握。

#### 4.2.5 小练习与答案

**练习 1**:`CHECK_SHAPE(k, batch_size, seqlen_knew, num_heads_k, head_size_og)` 校验的是原始 k 还是重排后的形状?为什么 k 不需要参与 GQA 重排?

**答案**:校验的是原始形状——k 的校验发生在 GQA 重排(383-391)之后的第 454 行,但重排只作用于 q;k 本来就是 `(b, knew, h_k, d)` 的 KV 侧张量,头数已是 `num_heads_k`,重排只是让 q 的"头"伪装成"序列",KV 侧无需变化。

**练习 2**:为什么 `if (seqlen_q == 1) { is_causal = false; }`(decode 单 token 时因果掩码无意义)?

**答案**:因果掩码的作用是阻止第 i 个查询 token 看到第 i 个之后的 KV。当只有 1 个查询 token(即最新 token)时,它按定义可以看到全部历史 KV,不存在"未来"可遮蔽,所以因果与非因果等价,直接关掉以简化 kernel 路径。这也与 Python 侧写死 `is_causal=False` 双重保险一致。

**练习 3**:若调用方把 `opt_k_new` 传成 `None` 而其余照常,函数还能跑吗?

**答案**:能。`k_` 是 `c10::optional`,`has_value()` 为假时整个 440-500 分支被跳过,`params` 中不设置残余/新 token 字段;同时第 502 行 `params.is_seqlens_k_cumulative = !(seqlens_k_.has_value())` 会置 true,表示 `cu_seqlens_k` 语义为"累计长度"。这对应纯 packed 缓存、无残余区的调用方式。

---

### 4.3 run_mha_fwd / run_kvcache_qpack:运行时 dispatch 与"静默无操作"陷阱

#### 4.3.1 概念说明

Python 层解决了 `num_bits` 的编译期绑定,但 `quant_mode`(k-channel/k-tensor)和 `group_size`(128/64/32)是三个模板参数中的另外两个,它们的运行时值要到 C++ 层才能读入 `params`。`run_mha_fwd` 与 `run_kvcache_qpack` 就是两段 `if` 链,负责"运行时值 → 模板实例"的最后一步映射。

这段代码最重要的工程事实是:**并非所有分支都被启用**。当前仓库只编译了 k-channel 模式下 group_size 为 128/32 的实例,k-tensor 模式的所有分支都被注释掉了。而 `if/else-if` 链落空时**不会报错、也不会打印任何信息**——函数直接返回,一个 kernel 都不启动。这就是 u2-l3 结尾"传错组合会静默无操作"的根源,现在我们在源码层看到了它的确切位置。

#### 4.3.2 核心流程

```text
run_mha_fwd<num_bits>(params, stream, force_split_kernel)
  ├─ num_splits<=1 且未强制 split → run_mha_fwd_<half,128,false>(非量化路径,本链不会走)
  └─ 否则(split 路径):
       quant_mode == "k-channel"?
         ├─ group_size==128 → dispatch<half_t, 128, false, 1, num_bits, 128>  ✅启用
         ├─ group_size==64  → (被注释,❌落空)
         └─ group_size==32  → dispatch<half_t, 128, false, 1, num_bits, 32>   ✅启用
       否则(k-tensor):
         └─ 128/64/32 三个分支全部被注释                                   ❌全落空
```

模板实参的含义(位置对应):`<元素类型, Headdim, Is_causal, quant_mode(1=k-channel,0=k-tensor), num_bits, group_size>`。声明见 [flash.h:203-205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L203-L205)。

#### 4.3.3 源码精读

**decode 侧的 dispatch**:

[decode_api.cpp:196-216](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L196-L216) — `run_mha_fwd` 的核心 if 链。k-channel 的 128/32 分支各有一行真实调用;group_size==64 分支(k-channel)和整个 else(k-tensor)分支只有注释。

```cpp
if (params.quant_mode == "k-channel") {
    if (params.group_size == 128) {
        run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, num_bits, 128>(params, stream);
    } else if (params.group_size == 64) {
        // run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, num_bits, 64>(...);
    } else if (params.group_size == 32) {
        run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, num_bits, 32>(params, stream);
    }
} else {
    // k-tensor 的三个分支全部被注释
}
```

**pack 侧的 dispatch**(注意分支顺序不同,启用的组合相同):

[decode_api.cpp:219-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L219-L238) — `run_kvcache_qpack` 只在 k-channel 下启用 group_size 32/128 两个实例。

**dispatch 与实例化必须配对**。if 链里写了一行调用,就必须在某个编译单元里有对应的显式实例化,否则链接时报"undefined symbol"。证据在 genfile:

[flash_fwd_split_hdim128_fp16_sm80_4bit.cu:7-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu#L7-L14) — 4-bit 解码 kernel 的实例化清单:k-tensor(第 4 个模板参为 0)三行全注释;k-channel 128/32 两行启用,64 注释。

```cpp
// template void run_mha_fwd_splitkv_dispatch<half_t, 128, false, 0, 4, 128>(...);  // k-tensor,注释
template void run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, 4, 128>(...);  // ✅
// template void run_mha_fwd_splitkv_dispatch<half_t, 128, false, 1, 4, 64>(...);   // 注释
template void run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, 4, 32>(...);   // ✅
```

两侧的启用状态完全一致——这构成一个自洽的"配置矩阵":**k-channel × group_size∈{128,32} × num_bits∈{2,4}**,正是 u1-l2 从 setup.py 源列表推出的结论,现在从 dispatch 侧再次验证。这也预告了 u7-l3 的扩展实践:新增 group_size=64 需要同时解开两处注释(dispatch 行 + genfile 实例化行),缺一不可。

**dispatch 终点**。以解码链为例:

[flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137) — `run_mha_fwd_splitkv_dispatch` 把 6 个模板参数组装成完整的 `Flash_fwd_kernel_traits<...>` 类型,固定 `kBlockM=16`(decode 单 token 的 tile 行数)与 `kBlockN=256`,交给 `run_flash_splitkv_fwd`。后者([flash_fwd_launch_template.h:76-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L76-L104))依次启动 `flash_fwd_residual_kernel`(残余区)与 `flash_fwd_splitkv_kernel`(主 split),再按 `num_splits>1` 启动 combine kernel——kernel 内部实现属于第五单元,本讲到此止步。

#### 4.3.4 代码实践

**实践目标**:亲眼确认"静默无操作"陷阱,理解为什么它危险。

**操作步骤**:

1. 有 GPU 环境时,复制 [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) 的张量构造部分,把 `quant_mode` 从 `"k-channel"` 改成 `"k-tensor"`(形状保持 k-channel 布局),其余不变,运行一次;
2. 观察输出:不会有任何报错或警告,但 `fwd_kvcache_int` 返回的 `out_bit` 是 `torch::empty_like` 分配的**未初始化内存**([decode_api.cpp:398-399](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L398-L399)),通常是巨大的垃圾值或 NaN,与参考注意力的误差大到离谱;
3. 对照 [decode_api.cpp:207-215](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L207-L215) 找到原因:k-tensor 的 else 分支里没有任何启用的调用。

**需要观察的现象**:`quant_mode="k-tensor"` 时输出为垃圾值但无异常;`quant_mode="k-channel"` 且 `group_size=64` 时同样落空;`group_size=128/32` 时正常。

**预期结果**:理解"参数合法但不被支持"与"参数非法"是两类错误——后者被 TORCH_CHECK 拦截,前者静默通过。二次开发时的自查手段:调参前先数一遍 dispatch 链里启用的组合。

**待本地验证**:本实践的运行现象需 GPU 环境验证;无 GPU 时改为源码练习——统计 [decode_api.cpp:199-216](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L199-L216) 与 [decode_api.cpp:221-237](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L221-L237) 中未注释的 `run_*` 调用行,写出启用配置矩阵(应为 2 个函数 × 2 种 group_size × 2 种 num_bits = 各 4 个实例)。

#### 4.3.5 小练习与答案

**练习 1**:如果只解开 genfile 里 `group_size=64` 的实例化注释,而不解开 `run_mha_fwd` 里的 dispatch 注释,会发生什么?

**答案**:能编译通过、链接成功(实例化只是多生成一份代码),但运行时永远走不到它——dispatch 的 if 链里 64 分支仍是空的。反过来只解 dispatch 不解实例化,则链接期直接报 undefined reference。所以**dispatch 行与实例化行必须成对解开**,这是 u7-l3 扩展实践的核心操作。

**练习 2**:为什么 `run_mha_fwd` 里保留 `params.num_splits <= 1 && !force_split_kernel` 的非 split 分支?

**答案**:这是从原版 FlashAttention 继承的结构:非 split 路径(`run_mha_fwd_<half_t, 128, false>`,对应 genfile 里的 `flash_fwd_hdim128_fp16_sm80.cu`)服务"Q 较长、无需切 KV"的常规前向。本项目的 decode 链因"要向 KV cache 追加新 token"而永远 `force_split_kernel=true`,该分支成为不会被走到但保留结构的遗产,也是理解本项目与 FA 上游关系的一个线索。

---

### 4.4 kvcache_qpack<num_bits>:量化打包入口与被折叠的 batch 维

#### 4.4.1 概念说明

`kvcache_qpack` 是 `kvcache_pack_int2/4` 背后的函数,负责 prefill 阶段把 FP16 的 K/V 量化打包进四个输出张量。它与 `mha_fwd_kvcache` 结构相似(校验 → 填 params → dispatch),但有两个独特点:

1. **batch 维被 Python 侧折叠了**。Python 入口先把 `(b, s, h, d)` reshape 成 `(b*s, h, d)`(varlen 风格),于是 C++ 侧 `k.size(0)` 不再是 batch 数——**batch 数必须从 `cu_seqlens_k`(累计序列长度表)反推**;
2. 因此 batch stride 也无法直接取 `k.stride(0)`,要用逻辑序列长手工计算。

#### 4.4.2 核心流程

```text
Python: k_cache.reshape(b*s, h, d), v_cache.reshape(b*s, h, d)
        cu_seqlens_k = [0, s, 2s, ..., b*s]  (int32)
  ▼
kvcache_qpack<4>(k, k_pack, k_params, v, v_pack, v_params,
                 block_table_, cu_seqlens_k, max_seqlen_k, quant_mode, group_size)
  ├─ ① 校验:k/v 是 fp16/bf16;cu_seqlens_k 是 int32、连续、在 GPU;
  │     k/v 最后一维连续;block_table(若 paged)约束
  ├─ ② 尺寸提取:batch_size = cu_seqlens_k.numel() - 1;h、d 从 k.sizes() 取
  ├─ ③ set_params_fprop_qpack:填指针与 stride(非 paged 时 batch stride 手工算)
  └─ ④ max_seqlen_k > 0 时 run_kvcache_qpack<4> → dispatch → run_flash_qpack
       启动 flash_qpack_kernel,grid = (seqlen_k 的块数, b, h)
```

#### 4.4.3 源码精读

**校验段**:

[decode_api.cpp:612-622](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L612-L622) — dtype 白名单(fp16/bf16)、`cu_seqlens_k` 的 int32 与连续性、`CHECK_DEVICE` 三连、k/v 的 `stride(-1)==1`。与 `mha_fwd_kvcache` 的校验风格完全一致,可对照阅读体会"同一套防御模式"。

**batch 数的恢复**——本函数最关键的一行:

[decode_api.cpp:635-638](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L635-L638) — `batch_size = cu_seqlens_k.numel() - 1`:累计长度表有 b+1 个边界值,故 batch 数 = 元素数减一。头数与头维改从 `sizes[1]`/`sizes[2]` 取(而非 decode 入口的 `sizes[2]`/`sizes[3]`),因为第 0 维已经被折叠成 b*s。

```cpp
const int batch_size  = cu_seqlens_k.numel() - 1;
int num_heads         = paged_KV ? sizes[2] : sizes[1];
const int head_size   = paged_KV ? sizes[3] : sizes[2];
```

配套的 Python 侧构造(测试文件里的用法):

[evaluation/test.py:74-75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L74-L75) — `torch.arange(0, (batch_size+1)*seqlen_k_pack, seqlen_k_pack)` 生成 `[0, s, 2s, ..., b*s]`,正是"每个序列从哪开始"的累计表。

[bit_decode_interface.py:23-24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L23-L24) — 折叠动作本身:`K_unpad = k_cache.reshape(batch_size * seqlen_k, nheads_k, d)`。reshape 是零拷贝的视图操作,只为让张量形状匹配"变长批"约定。

**batch stride 的手工计算**:

[decode_api.cpp:579-586](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L579-L586) — `set_params_fprop_qpack` 内,非 paged 时 `params.k_batch_stride = seqlen_k * k.size(-2) * k.size(-1)`。为什么不能像 decode 入口那样直接 `k.stride(0)`?因为 `K_unpad` 的 `stride(0)` 是 `h*d`(下一"行"的距离),而 kernel 需要"下一个 batch 的同位置"的距离 `s*h*d`,只能用传入的 `max_seqlen_k` 乘出来。

```cpp
if (page_kv) params.k_batch_stride = k.stride(0);
else params.k_batch_stride = seqlen_k * k.size(-2) * k.size(-1);
```

注意一个隐患:这里的 `seqlen_k` 是调用方传入的 `max_seqlen_k`(逻辑序列长)。若调用方 reshape 时用的 `seqlen_k` 与此处不一致,stride 就会算错——好在 Python 入口的两个值来自同一处([bit_decode_interface.py:21](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21) 从 `k_cache.shape` 解出再原样传回),接口约定保证了自洽。

**启动与守卫**:

[decode_api.cpp:679-682](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L679-L682) — 只有 `max_seqlen_k > 0` 才启动 kernel,空序列直接返回。之后进入 4.3 已读的 `run_kvcache_qpack<num_bits>` dispatch,终点是 [flash_fwd_launch_template.h:181-198](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L181-L198) 的 `run_flash_qpack`:`grid=(num_n_block, b, h)`,共享内存超过 48KB 时用 `cudaFuncSetAttribute` 放开限制(kernel 细节在第四单元)。

#### 4.4.4 代码实践

**实践目标**:验证"折叠 batch 维 + cu_seqlens_k 恢复 batch 数"这套约定,理解形状在两个视角间的转换。

**操作步骤**:

1. 运行以下独立 PyTorch 片段(仅 CPU,无需编译扩展,**示例代码**):

```python
import torch

b, s, h, d = 2, 100, 8, 128
k_cache = torch.randn(b, s, h, d, dtype=torch.float16)

# 复刻 bit_decode_interface.py:23 的折叠
K_unpad = k_cache.reshape(b * s, h, d)

# 复刻 test.py:74 的累计长度表
cu_seqlens_k = torch.arange(0, (b + 1) * s, s, dtype=torch.int32)

# 复刻 decode_api.cpp:635 的 batch 恢复
batch_size = cu_seqlens_k.numel() - 1
# 复刻 decode_api.cpp:580 的 batch stride 手工计算
k_batch_stride = s * K_unpad.size(-2) * K_unpad.size(-1)

print("batch_size =", batch_size)            # 期望 2
print("stride(0) =", K_unpad.stride(0))      # 期望 h*d = 1024
print("k_batch_stride =", k_batch_stride)    # 期望 s*h*d = 102400
# 验证:手工 stride 确实指向下一个 batch 的同位置
assert torch.equal(K_unpad.flatten()[k_batch_stride: k_batch_stride + s*h*d],
                   k_cache[1].flatten())
print("stride 语义验证通过")
```

2. 回答:`num_heads`/`head_size` 为什么从 `sizes[1]`/`sizes[2]` 取而不是 `sizes[2]`/`sizes[3]`?

**需要观察的现象**:三行打印值与注释一致;assert 通过。

**预期结果**:输出 `batch_size = 2`、`stride(0) = 1024`、`k_batch_stride = 102400`、`stride 语义验证通过`。若把 `k_batch_stride` 误用为 `K_unpad.stride(0)`,assert 会失败——这正是 C++ 侧必须手工计算的原因。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `kvcache_qpack` 不像 `mha_fwd_kvcache` 一样直接接收 `(b, s, h, d)` 的 4 维张量?

**答案**:这是从 FlashAttention 的 varlen(变长序列)接口继承的形态:折叠成 `(total_tokens, h, d)` 后,配合 `cu_seqlens_k` 就能自然支持"批内各序列长度不等"的打包(每个序列的边界由累计表给出)。本项目当前调用方都传等长序列,但接口形态保留了这种通用性。

**练习 2**:对照两个入口:decode 侧 `set_params_fprop` 从 `k_pack.stride(0)` 取 batch stride([decode_api.cpp:95-99](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L95-L99)),qpack 侧却要手工计算。张量形状的什么差异导致了这一点?

**答案**:decode 侧的 `k_pack` 是规整的 4 维 `(b, seqlen/pack_nums, h, d)`,`stride(0)` 天然就是 batch 间距;qpack 侧输入的 `k` 已被折叠成 3 维 `(b*s, h, d)`,`stride(0)` 退化为 token 间距,必须用 `seqlen_k × h × d` 重建。

**练习 3**:`kvcache_qpack` 的 `TORCH_CHECK(head_size % 8 == 0, ...)` 比 decode 入口多一条(head_size 为 8 的倍数)。结合 GQA 重排条件里的 `head_size_og % 8 == 0`,猜测这个约束的动机。

**答案**:两个入口都要求头维是 8 的倍数,指向同一个底层需求:kernel 按向量化宽度访问数据(如 128 bit = 8 个 fp16),头维不是 8 的倍数会导致跨行访问不对齐。这是 CUDA kernel 常见的对齐性约束,在 qpack 里显式检查,在 decode 侧则作为重排的前提条件出现。

## 5. 综合实践

**错误注入实验矩阵**:把本讲三个模块串成一次系统性检查。设计一张表,列出 6 种"故意传错"的调用,先**预测**错误在哪一层、以什么形式出现,再(有环境时)验证:

| # | 注入方式 | 你的预测(层 + 现象) | 参考答案 |
|---|---|---|---|
| 1 | `num_bits=3` 调 `fwd_kvcache_int` | ? | Python 层 `ValueError`([bit_decode_interface.py:103-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L103-L104)) |
| 2 | 给 `fwd_kvcache_int4` 传 CPU 上的 q | ? | C++ 层 `CHECK_DEVICE` 抛 `"q must be on CUDA"` |
| 3 | k_new 形状错一位 | ? | C++ 层 `CHECK_SHAPE` 抛形状不匹配消息 |
| 4 | `quant_mode="k-tensor"`(形状不变) | ? | **无任何报错**,返回未初始化的垃圾 out(4.3.4) |
| 5 | `group_size=64` + k-channel | ? | 同上,dispatch 落空 |
| 6 | 少传最后一个 `num_splits` 参数 | ? | pybind11 抛 `TypeError`(参数个数不符) |

操作:先盖住右列独立填写"你的预测"列;有 GPU 环境时逐条运行验证(1、6 可在纯 CPU + 已编译扩展上验证),无 GPU 时对照本讲正文核对。**待本地验证**(第 2-5 条需 GPU)。完成后你应当能准确说出:哪些错误在 Python 层被拦、哪些在 C++ 校验层被拦、哪些根本不被拦——这三种"拦截面"的分布,就是本讲对绑定层设计的完整画像。

## 6. 本讲小结

- `PYBIND11_MODULE` 只导出 4 个函数,它们是 `kvcache_qpack<2/4>` 与 `mha_fwd_kvcache<2/4>` 四个模板实例;`num_bits` 作为模板参数在编译期固化,Python 层因此必须按位宽分流函数名,并按位置严格对齐 26 个参数。
- `mha_fwd_kvcache` 的执行骨架是"校验 → GQA 重排 → 填 params → 强制 split → 逆重排返回 5 元组";`CHECK_DEVICE/SHAPE/CONTIGUOUS` 与一系列 `TORCH_CHECK` 构成宿主端的防御层。
- GQA 的 `seqlenq_ngroups_swapped` 把 `(b,1,h,d)` 的 q 重排成 `(b,ngroups,h_k,d)`,用"头换序列"提高 decode tile 利用率,输出再做逆重排。
- `run_mha_fwd`/`run_kvcache_qpack` 是"运行时值 → 编译期模板实例"的 if 链分发;当前仅启用 **k-channel × group_size∈{128,32} × num_bits∈{2,4}**,其余分支与 genfile 实例化成对被注释,落空时**静默无操作**。
- `kvcache_qpack` 因 Python 侧折叠了 batch 维,须从 `cu_seqlens_k.numel()-1` 恢复 batch 数、手工计算 batch stride,这是 varlen 接口形态的代价。
- 签名中的 `residual_block_size` 在函数体内未被消费,实际块大小由 [kernel_traits.h:75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L75) 的编译期常量决定。

## 7. 下一步学习建议

本讲我们多次把 `Flash_fwd_params params` 当作"填好就传走"的黑盒。下一讲 **u3-l2(Flash_fwd_params:贯穿 CPU 与 GPU 的参数结构体)** 将打开它:逐字段理解 `K_pack_ptr`/`k_params_dim_stride` 等非对称 stride 的来源、`cu_seqlens_k` 与残余长度的语义差异。再往后的 **u3-l3** 深入 `set_params_splitkv` 背后的 `num_splits_heuristic` 算法。如果你更想先看 kernel 侧如何消费这些参数,也可以跳到第五单元的 u5-l1(kernel_traits),但建议按顺序先读完 u3-l2——后面所有 kernel 代码的索引数学都依赖对 params 字段的理解。
