# Paged KV Cache 布局与 append

## 1. 本讲目标

上一讲（u3-l1）我们从**概念上**建立了「为什么 LLM 推理需要把 KV-Cache 分页存储」，并介绍了页表三件套与 `get_seq_lens` 公式。本讲从概念落到**具体的数据结构与写入代码**，读完本讲你应当能够：

- 说出 NHD 与 HND 两种 KV 布局在张量维度顺序上的区别，以及它们如何决定 kernel 内部的 stride 计算；
- 用「逻辑序列 → 页表 → 物理页」这条链路，解释 `kv_indices` / `kv_indptr` / `kv_last_page_len` 三个数组如何协同工作；
- 独立写出一段最小代码：分配分页 KV-Cache 张量、构造页表、调用 `append_paged_kv_cache` 写入若干 token，并把写入的 K/V 读回来校验。

本讲只覆盖 KV-Cache 的「**存储布局 + 写入（append）**」这一面，不涉及读取侧的 attention 计算（那是下一讲 u3-l3 decode wrapper 的内容）。

## 2. 前置知识

- **张量的 stride（步长）**：一个多维张量在内存里是一维连续存放的，stride 告诉你「沿某一维前进一格，内存地址要跳过多少个元素」。例如形状 `[2,3,4]` 的连续张量，stride 是 `[12,4,1]`。本讲会大量遇到 `stride_page` / `stride_n` / `stride_h` 这类命名。
- **CSR 风格的 indptr（行指针）**：用一个长度为 `batch+1` 的数组描述「每段有多长」。第 `i` 段的范围是 `[indptr[i], indptr[i+1])`。u3-l1 已用过它描述变长请求。
- **ragged tensor（参差张量）**：若干长度不一的序列「拼接」成一个大一维张量，靠 indptr 还原每段边界。`append_key`/`append_value` 就是这种形态。
- **页（page）**：借鉴操作系统的虚存分页思想。把一整条序列切成固定大小的「页」，每页装 `page_size` 个 token 的 K/V；序列在物理显存里不必连续，靠页表把逻辑顺序映射回物理页。详见 u3-l1。
- **GQA/MHA 约定**：注意力有 `num_qo_heads` 个 query 头与 `num_kv_heads` 个 KV 头，通常 `num_qo_heads` 是 `num_kv_heads` 的整数倍（Grouped Query Attention）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/page.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py) | 用户直接调用的 Python API：`append_paged_kv_cache`、`get_batch_indices_positions`、`get_seq_lens`，以及 torch custom op 注册 |
| [include/flashinfer/page.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh) | 框架无关的 CUDA kernel 头文件：定义 `paged_kv_t` 结构体、append kernel 与 launcher |
| [include/flashinfer/layout.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/layout.cuh) | 定义 C++ 侧的 `QKVLayout` 枚举（kNHD=0 / kHND=1）与 stride 工具 |
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py) | Python 侧 `TensorLayout` 枚举、`_check_kv_layout`、`_unpack_paged_kv_cache` 等布局工具 |
| [flashinfer/jit/page.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/page.py) | page 模块的 JIT 生成器 `gen_page_module` |
| [csrc/page.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/page.cu) | launcher：把 TVM-FFI 传入的张量校验后组装成 `paged_kv_t` 并启动 kernel |
| [tests/attention/test_page.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_page.py) | 端到端测试，可作为可运行样例参考 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**4.1 KV 布局（NHD/HND）**、**4.2 页表（逻辑→物理映射）**、**4.3 append 写入流程**。三者层层递进：布局决定了「一个元素在张量里的偏移怎么算」，页表决定了「逻辑 token 落在哪个物理页」，append 则把这两者串起来执行写入。

### 4.1 KV 布局：NHD 与 HND

#### 4.1.1 概念说明

分页 KV-Cache 把每个物理页想象成一个「小盒子」，盒子里装着 `page_size` 个 token、每个 token 在 `num_kv_heads` 个头上有 `head_dim` 维的 K（和 V）。问题来了：这个小盒子的三个维度（token 序号 N、头 H、特征 D）以什么顺序摆放？

FlashInfer 提供两种合法顺序，由字符串 `kv_layout` 选择：

- **NHD**（`"NHD"`，默认值）：`[max_num_pages, page_size, num_kv_heads, head_dim]` —— token 在前，头在中间。
- **HND**（`"HND"`）：`[max_num_pages, num_kv_heads, page_size, head_dim]` —— 头在前，token 在中间。

为什么需要两种？因为不同模型/框架历史上习惯的内存排布不同。NHD 让「同一个 token 的所有头」相邻（适合一次读一个 token 的全部头）；HND 让「同一个头的所有 token」相邻（适合一个头连续扫一段序列）。两者在数学上等价，只是 stride 不同。**关键设计**：FlashInfer 的 kernel 不为两种布局写两份代码，而是把布局抽象成一个枚举值，在运行时据此选用不同的 stride——这正是上单元 u2 讲的「把组合参数推迟到运行期」的思想。

#### 4.1.2 核心流程

对一个 4D 的 K-Cache 张量 `k_cache`（去掉第 0 维 `max_num_pages` 后剩 3 维），布局只影响后三维的解读：

```text
布局 NHD:  k_cache[page, n(token), h(head), d(feature)]
布局 HND:  k_cache[page, h(head),  n(token), d(feature)]
                                d 维永远是最后一维（最内层、必须连续）
```

给定一个 `(page, head, entry, feat)` 四元组，元素在整个 k_cache 一维缓冲里的**线性偏移**为：

\[
\text{offset} = \text{page}\cdot\text{stride\_page} + \text{head}\cdot\text{stride\_h} + \text{entry}\cdot\text{stride\_n} + \text{feat}
\]

其中 `stride_page = num_heads * page_size * head_dim`（两种布局相同，因为页内总元素数一样），而 `stride_n` 与 `stride_h` 因布局而异：

| 布局 | stride_n（沿 token 步长） | stride_h（沿 head 步长） |
|------|--------------------------|--------------------------|
| NHD  | `num_kv_heads * head_dim` | `head_dim` |
| HND  | `head_dim`                | `page_size * head_dim` |

可以看到：NHD 时头是「最小单位」（stride_h 最小），HND 时 token 是「最小单位」（stride_n 最小）。这与上面「谁的相邻关系」一致。

#### 4.1.3 源码精读

Python 侧用枚举把字符串映射成整数，再传给底层：

- [flashinfer/utils.py:L45-L47](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L45-L47) 定义 `TensorLayout`，`NHD=0`、`HND=1`。
- [flashinfer/utils.py:L153-L155](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L153-L155) `_check_kv_layout` 用 `hasattr` 校验字符串只能是 `"NHD"` 或 `"HND"`，否则抛 `KeyError`。
- [flashinfer/page.py:L518-L529](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L518-L529) `append_paged_kv_cache` 末尾把字符串转成 `TensorLayout[kv_layout].value`（即 0 或 1）传给 kernel。

C++ 侧用同名枚举接收这个整数，并据此算 stride：

- [include/flashinfer/layout.cuh:L28-L33](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/layout.cuh#L28-L33) `enum class QKVLayout { kNHD = 0, kHND = 1 }`，与 Python 枚举值严格对应。
- [include/flashinfer/page.cuh:L143-L144](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L143-L144) 是布局决定 stride 的核心两行：`stride_n = (HND ? head_dim : num_heads*head_dim)`、`stride_h = (HND ? page_size*head_dim : head_dim)`。这正是上表那两条规则的源码化身。

> 小贴士：FlashInfer 还允许「K、V 各一张 4D 张量」的 **tuple 形态**与「K/V 叠在第 1 维成一张 5D 张量」的 **stacked 形态**。Python 侧用 `_unpack_paged_kv_cache` 统一拆成两张 4D 图（见 [flashinfer/utils.py:L169-L189](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L169-L189)），其中 `_expand_5d` 在第 1 维 `unbind` 出 K/V，`_expand_4d` 处理 `page_size==1` 的退化情形。所以底层 kernel 永远只看到「两张 4D 张量 + 一个布局枚举」，形态多样性被 Python 层吃掉了。

#### 4.1.4 代码实践

**实践目标**：直观感受两种布局下 stride 的差异。

1. 用 PyTorch 构造两种布局的同一份 k_cache（`max_num_pages=2, page_size=4, num_kv_heads=8, head_dim=16`）。
2. 打印它们的 `.stride()`。
3. 对照源码公式手算 stride_n、stride_h，核验是否一致。

```python
# 示例代码（仅演示布局，不调用 FlashInfer）
import torch
max_num_pages, page_size, num_kv_heads, head_dim = 2, 4, 8, 16

nhd = torch.empty(max_num_pages, page_size, num_kv_heads, head_dim)
hnd = torch.empty(max_num_pages, num_kv_heads, page_size, head_dim)
print("NHD stride:", nhd.stride())   # 预期 (512, 128, 16, 1)
print("HND stride:", hnd.stride())   # 预期 (512, 64, 16, 1)
```

**需要观察的现象**：
- 两种布局第 0 维 stride 都是 `512 = page_size*num_kv_heads*head_dim`（`stride_page`，与布局无关）。
- NHD 的 `stride_n=128`（= `num_kv_heads*head_dim`），HND 的 `stride_n=16`（= `head_dim`）。
- 最后一维 stride 恒为 1（最内层连续，这是 `CHECK_LAST_DIM_CONTIGUOUS` 的要求）。

**预期结果**：手算与打印一致，且与 [page.cuh:L143-L144](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L143-L144) 的公式吻合。（本步骤不依赖 GPU，可在纯 CPU 上验证。）

#### 4.1.5 小练习与答案

**练习 1**：若 `page_size=1`，NHD 与 HND 的 4D 形状会变成什么？为什么 `_expand_4d` 还要专门处理这种情况？

**答案**：`page_size=1` 时 NHD 为 `[P,1,H,D]`、HND 为 `[P,H,1,D]`。有些上游框架在 `page_size==1` 时会省掉那一维，传进来的是 3D 张量 `[P,H,D]`。`_expand_4d` 根据 `kv_layout` 在正确位置 `unsqueeze` 补回那一维（NHD 补在倒数第 3 维、HND 补在倒数第 2 维），让底层始终看到统一的 4D。

**练习 2**：为什么 csrc 强制要求 K-cache 与 V-cache 的 stride 完全相同？

**答案**：见 [csrc/page.cu:L74-L78](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/page.cu#L74-L78)，`std::equal(k_strides..., v_strides...)` 做了断言。因为 `paged_kv_t` 结构体只存一份 stride，K、V 共用同一组 stride（只是 `k_data`/`v_data` 起始指针不同）。强制两者排布一致，才能用一套寻址逻辑同时写 K 和 V。

---

### 4.2 页表：从逻辑序列到物理页

#### 4.2.1 概念说明

u3-l1 介绍了页表三件套的名字，本讲讲清它们**如何协同完成映射**。设一个 batch 有 `batch_size` 个请求，KV-Cache 物理页池共有 `max_num_pages` 个页。描述「每个请求占用了哪些物理页」需要三个数组：

- **`kv_indices`（页索引数组，长度 = 总页数）**：按请求顺序依次列出每个请求占用的物理页编号。例如请求 0 用了物理页 `{0,3,5}`，请求 1 用了 `{1}`，则 `kv_indices = [0,3,5,1,...]`。**它就是页表本体**——告诉系统「第 k 个被占用的逻辑页槽对应哪个物理页」。
- **`kv_indptr`（页指针，长度 = `batch_size+1`）**：CSR 风格，划出 `kv_indices` 中哪一段属于哪个请求。`kv_indices[kv_indptr[b] : kv_indptr[b+1]]` 就是请求 `b` 用到的全部物理页。
- **`kv_last_page_len`（每请求最后一页的有效长度，长度 = `batch_size`）**：序列长度通常不是 `page_size` 的整数倍，最后一个页只装了一部分。这个数组记录每个请求最后一页实际装了多少个 token，取值范围 `[1, page_size]`。

这三个数组**共同**等价于「每个请求的序列长度」。事实上 u3-l1 给过反推公式，这里再从源码确认。

#### 4.2.2 核心流程

**① 由页表反推序列长度**：

请求 `b` 占用了 `num_pages_b = kv_indptr[b+1] - kv_indptr[b]` 个页。其中前 `num_pages_b - 1` 个页是**满的**（各 `page_size` 个 token），最后一个页只有 `kv_last_page_len[b]` 个 token。故：

\[
\text{seq\_len}_b = (\text{num\_pages}_b - 1)\times\text{page\_size} + \text{kv\_last\_page\_len}[b]
\]

**② 由「逻辑位置 position」定位「物理地址」**：

给定请求 `b` 内部第 `pos` 个 token（`pos` 从 0 计），它在物理页池里的绝对页内位置是 `kv_indptr[b]*page_size + pos`（把请求 `b` 的起点对齐到页边界）。对它做 `page_size` 的 divmod：

```text
global_offset = kv_indptr[b] * page_size + pos
page_iter     = global_offset / page_size     # 在 kv_indices 里的下标
entry_idx     = global_offset % page_size      # 页内的 token 槽位
page_idx      = kv_indices[page_iter]          # 真正的物理页编号
# 最终元素偏移 = page_idx*stride_page + head*stride_h + entry_idx*stride_n + feat
```

注意 `page_iter` 只是「在 `kv_indices` 数组里的下标」，还要再用 `kv_indices[page_iter]` 取一次才得到物理页号——这一层间接正是「页表」的本质。

下图是一个 `batch_size=2`、`page_size=4` 的例子，请求 0 长 6（占 2 页：物理页 0、3），请求 1 长 3（占 1 页：物理页 5）：

```text
kv_indptr          = [0, 2, 3]        # 请求0占 kv_indices[0:2]，请求1占 kv_indices[2:3]
kv_indices         = [0, 3, 5]        # 物理页编号
kv_last_page_len   = [2, 3]           # 请求0最后一页2个；请求1最后一页3个

请求0: pos=0..3 -> page_iter=0 -> 物理页0 ; pos=4,5 -> page_iter=1 -> 物理页3
请求1: pos=0..2 -> page_iter=2 -> 物理页5
seq_len: 请求0 = (2-1)*4+2 = 6 ; 请求1 = (1-1)*4+3 = 3   ✓
```

#### 4.2.3 源码精读

**反推序列长度**的源码实现，注意那个 `clamp(... -1, min=0)` 是为「空请求（0 页）」兜底：

- [flashinfer/page.py:L326-L349](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L326-L349) `get_seq_lens`：`torch.clamp(kv_indptr[1:]-kv_indptr[:-1]-1, min=0)*page_size + kv_last_page_len`。当某请求页数为 0 时，`num_pages-1 = -1`，clamp 成 0，避免负长度。

C++ 侧 `paged_kv_t::get_length` 是同一公式（见 [include/flashinfer/page.cuh:L147-L152](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L147-L152)），并额外处理「该请求 0 页（`indptr[b+1]==indptr[b]`）则长度为 0」。

**定位物理地址**的源码有两层间接，值得逐行看：

- [include/flashinfer/page.cuh:L161-L165](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L161-L165) `get_elem_offset`：纯 stride 线性组合，对应公式里的「最终元素偏移」。注意它接收的 `page_idx` 已是**物理页号**。
- [include/flashinfer/page.cuh:L179-L182](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L179-L182) `get_k_ptr`：里面那行 `__ldg(indices + page_iter)` 就是「页表查找」——把 `page_iter`（逻辑页槽）翻译成物理页号，再喂给 `get_elem_offset`。`__ldg` 是「只读纹理缓存」读取，加速页表的随机访问。V 侧对称有 `get_v_ptr`（[L200-L203](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L200-L203)）。

`paged_kv_t` 结构体本身把上述所有信息打包成一个可拷贝到 device 的小对象（[include/flashinfer/page.cuh:L37-L59](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L37-L59)）：数据指针 `k_data/v_data`、页表 `indices/indptr/last_page_len`、以及维度与 stride。它是 kernel 访问 KV-Cache 的唯一入口。

#### 4.2.4 代码实践

**实践目标**：用 Python 手算一个 token 的物理位置，建立对页表的肌肉记忆。

```python
# 示例代码（纯 CPU 推演，不调用 FlashInfer）
kv_indptr        = [0, 2, 3]
kv_indices       = [0, 3, 5]
kv_last_page_len = [2, 3]
page_size = 4

def locate(b, pos):
    global_offset = kv_indptr[b] * page_size + pos
    page_iter = global_offset // page_size
    entry_idx = global_offset % page_size
    page_idx  = kv_indices[page_iter]
    return page_iter, page_idx, entry_idx

# 请求0的第5个 token（pos=4）应落在 page_iter=1 -> 物理页3 -> 页内槽0
print(locate(0, 4))   # (1, 3, 0)
# 请求1的第2个 token（pos=1）应落在 page_iter=2 -> 物理页5 -> 页内槽1
print(locate(1, 1))   # (2, 5, 1)
```

**需要观察的现象**：`page_iter` 与 `page_idx` 是两个不同的数——前者是 `kv_indices` 的下标，后者才是真正的物理页号。
**预期结果**：与注释一致。把 `locate` 的逻辑与 [page.cuh:L351-L352](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L351-L352) 的 `divmod(... indptr[batch_indices[i]]*page_size + positions[i], page_iter, entry_idx)` 对照，会发现完全相同。

#### 4.2.5 小练习与答案

**练习 1**：若把 `kv_last_page_len` 的取值定为 `[0, page_size]` 而非 `[1, page_size]`，会带来什么歧义？

**答案**：当一个请求恰好占整数个满页时，最后一页「全满」既可以表达为「多占一个满页、`last_page_len=page_size`」，也可以表达为「不占这页、`last_page_len=0`」，页数与 last_page_len 的对应关系不再唯一。FlashInfer 选 `[1, page_size]`：满页就用一个 `last_page_len=page_size` 的页表示，保证 `(num_pages-1, last_page_len)` 这对表示唯一。

**练习 2**：`get_seq_lens` 里为何要 `clamp(min=0)`？

**答案**：空请求 `num_pages=0` 时，`num_pages-1=-1`，乘 `page_size` 得负数。`clamp(min=0)` 把它归零，再加上 `last_page_len`（空请求时通常也给 0），保证空请求序列长度为 0 而非负值。

---

### 4.3 append_paged_kv_cache 写入流程

#### 4.3.1 概念说明

`append_paged_kv_cache` 解决「**把新算出来的 K/V 写进分页 KV-Cache**」这件事——对应 u3-l1 提到的 **append 阶段**（prefill 后或 decode 每步后都要做）。它的输入是一段 **ragged** 的 K/V（若干请求的新 token 拼成一维），输出是就地修改的分页 cache。

它需要两类信息：
1. **要写什么**：`append_key` / `append_value`（ragged，形状 `[nnz, num_kv_heads, head_dim]`，`nnz` 是本批所有新 token 总数）、以及每个 token 属于哪个请求（`batch_indices`）和它在请求里的位置（`positions`）。
2. **写到哪**：页表三件套 `kv_indices` / `kv_indptr` / `kv_last_page_len`，加上 cache 张量本身。

注意一条**关键约定**（docstring 的 Note 里强调）：append **不负责分配新页**。它假设「要写入的空间已经在页表里分配好了」——即 `kv_indices`/`kv_indptr`/`kv_last_page_len` 已经把本次 append 的 token 计入了。换句话说，先由调度器决定「这些新 token 落在哪些物理页的哪些槽位」，更新页表，再调 append 执行实际拷贝。这样写入 kernel 就是个纯粹的「按映射做 scatter 拷贝」，逻辑简单、可进 CUDA Graph。

#### 4.3.2 核心流程

Python 入口 `append_paged_kv_cache` 的执行链：

```text
append_paged_kv_cache(K, V, batch_indices, positions, paged_kv_cache,
                      kv_indices, kv_indptr, kv_last_page_len, kv_layout)
   │
   ├─ _check_kv_layout(kv_layout)              # 校验只能是 "NHD"/"HND"
   ├─ _unpack_paged_kv_cache(cache, kv_layout) # tuple/stacked -> 两张 4D (k_cache, v_cache)
   ├─ TensorLayout[kv_layout].value            # 字符串 -> 整数 0/1
   └─ _append_paged_kv_cache_kernel(...)       # torch custom op
         │
         ├─ get_page_module()                  # @functools.cache 加载/编译 page 模块
         └─ module.append_paged_kv_cache(...)  # 经 TVM-FFI 进 C++
               │
               └─ csrc/page.cu: append_paged_kv_cache(...)
                     ├─ 校验形状/dtype/device/stride
                     ├─ 按布局抽 num_heads/page_size
                     ├─ 组装 paged_kv_t (含 stride)
                     └─ AppendPagedKVCache() -> kernel (page.cuh)
```

**kernel 内部**（`AppendPagedKVCacheKernel`）做的事很简单：每个线程块处理若干个 `(token, head)`，对第 `i` 个待写 token：

1. 由 `batch_indices[i]` 和 `positions[i]` 算出 `page_iter`、`entry_idx`（4.2 的定位逻辑）；
2. 由 `page_iter` 经 `kv_indices` 取物理页号，再由 stride 算出 k/v 的目标地址；
3. 用向量化 `memcpy`（一次搬 `vec_size` 个元素）把 `append_key[i, head]` 拷到 k 目标地址、`append_value[i, head]` 拷到 v 目标地址。

grid 维度按「SM 数 × 每 SM 占用块数」自适应，循环步进覆盖所有 `nnz` 个 token。

#### 4.3.3 源码精读

**Python 入口与约定**：

- [flashinfer/page.py:L402-L529](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L402-L529) `append_paged_kv_cache`。其中 docstring（[L428-L450](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L428-L450)）详述了 cache 两种形态对应的形状；Note（[L508-L512](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L508-L512)）声明了「空间需预先分配」的约定。
- [flashinfer/page.py:L79-L111](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L79-L111) `_append_paged_kv_cache_kernel`：被 `@register_custom_op("flashinfer::append_paged_kv_cache", mutates_args=("paged_k_cache","paged_v_cache"))` 注册为 torch custom op。`mutates_args` 告诉 `torch.compile`/CUDA Graph「这两个张量会被原地改写」，是正确性的关键。函数体把索引张量统一 `.int()`（int32 是底层 `IdType` 的默认类型）后转给 module。
- [flashinfer/page.py:L41-L43](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L41-L43) `get_page_module` 带 `@functools.cache`，对应 u2 讲的「进程内第一级缓存」——同进程内只编译加载一次 page 模块。

**JIT 生成器**：

- [flashinfer/jit/page.py:L21-L28](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/page.py#L21-L28) `gen_page_module`：注意它**没有参数**——因为 page kernel 不需要按 dtype/head_dim 做编译期特化（dtype 在 C++ 侧用 `DISPATCH_DLPACK_DTYPE_TO_CTYPE` 运行期派发，head_dim 用 `DISPATCH_HEAD_DIM`），所以只拷贝两个固定 `.cu` 源文件即可，是最简单的 `gen_*_module` 形态。

**launcher（csrc/page.cu）**：

- [csrc/page.cu:L61-L71](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/page.cu#L61-L71) 按 `kv_layout` 从 4D 张量的第 1/2 维抽出 `num_heads` 与 `page_size`——这是布局差异在 launcher 的唯一体现。
- [csrc/page.cu:L94-L100](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/page.cu#L94-L100) 用 `DISPATCH_DLPACK_DTYPE_TO_CTYPE` 按运行期 dtype 实例化 `paged_kv_t<c_type, int32_t>`，把数据指针、stride、页表指针都塞进去，再调 `AppendPagedKVCache`。

**kernel（include/flashinfer/page.cuh）**：

- [include/flashinfer/page.cuh:L334-L360](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L334-L360) `AppendPagedKVCacheKernel`。L351-L352 那行 `divmod` 把 `batch_indices[i]`+`positions[i]` 翻译成 `page_iter/entry_idx`；L353-L354 用 `get_k_ptr/get_v_ptr`（内含页表查找）拿到目标地址；L355-L358 做向量化 scatter 拷贝。外层 `for (i = cta_id; i < nnz; i += num_ctas)` 让 grid 自适应 token 数。
- [include/flashinfer/page.cuh:L404-L438](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L404-L438) `AppendPagedKVCache` launcher：用 `cudaOccupancyMaxActiveBlocksPerMultiprocessor` 估算每 SM 可同时驻留的块数，再把 grid 限制为「不超过 nnz 所需」，避免空转 block。

> 还有一个为 decode 优化的特化 kernel `AppendPagedKVCacheDecodeKernel`（[page.cuh:L298-L320](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L298-L320)）：每个请求只写最后一个 token 时，可直接由 `indptr`+`last_page_len` 算出唯一目标槽位，无需 `batch_indices`/`positions`。本讲的 Python 入口走的是通用 `AppendPagedKVCache` 路径（prefill/变长 append），decode 专用路径在其它 wrapper 里调用。

#### 4.3.4 代码实践

**实践目标**：构造一个最小分页 KV-Cache，append 两个请求的新 token，**读回校验**写入正确（仓库测试只验 nvfp4 路径，这里补一个对标准 append 的读回校验）。

> 前置：已按 u1-l2 安装 flashinfer 且能访问 CUDA 设备。以下改编自 [tests/attention/test_page.py:L82-L131](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_page.py#L82-L131)。

```python
import torch
import flashinfer

torch.manual_seed(0)
device = "cuda:0"

# 1) 问题规模：2 个请求，分别 append 5 和 3 个 token
num_kv_heads, head_dim, page_size = 8, 128, 4
kv_append_length = torch.tensor([5, 3], dtype=torch.int32, device=device)
nnz_kv = int(kv_append_length.sum().item())   # 8

# 2) 待写入的 K/V（ragged：8 个 token 拼成一维）
k_append = torch.randn(nnz_kv, num_kv_heads, head_dim, dtype=torch.float16, device=device)
v_append = torch.randn_like(k_append)
kv_append_indptr = torch.cat(
    [torch.zeros(1, dtype=torch.int32, device=device), kv_append_length.cumsum(0)]
).int()  # [0, 5, 8]

# 3) 页表：请求0 占 2 页(物理页 0,1)，请求1 占 1 页(物理页 2)
#    5 = (2-1)*4 + 1  -> 请求0 两页，最后一页 1 个
#    3 = (1-1)*4 + 3  -> 请求1 一页，最后一页 3 个
num_pages_per_req = torch.tensor([2, 1], dtype=torch.int32, device=device)
kv_page_indptr = torch.cat(
    [torch.zeros(1, dtype=torch.int32, device=device), num_pages_per_req.cumsum(0)]
).int()  # [0, 2, 3]
kv_page_indices = torch.arange(3, dtype=torch.int32, device=device)  # 用物理页 0,1,2
kv_last_page_len = torch.tensor([1, 3], dtype=torch.int32, device=device)

# 4) 由 indptr + last_page_len 算每个待写 token 的 (batch_index, position)
seq_lens = flashinfer.get_seq_lens(kv_page_indptr, kv_last_page_len, page_size)
batch_indices, positions = flashinfer.get_batch_indices_positions(
    kv_append_indptr, seq_lens, nnz_kv
)

# 5) 分配 KV-Cache（用 0 初始化，便于事后看出哪些位置被写过）
paged_kv_cache = torch.zeros(3, 2, page_size, num_kv_heads, head_dim,
                             dtype=torch.float16, device=device)  # stacked 5D, NHD

# 6) 执行 append
flashinfer.append_paged_kv_cache(
    k_append, v_append, batch_indices, positions, paged_kv_cache,
    kv_page_indices, kv_page_indptr, kv_last_page_len, kv_layout="NHD",
)

# 7) 读回校验：逐 token 找到它被写进哪个物理页/槽位，对比输入
bi = batch_indices.cpu(); pi = positions.cpu()
indptr = kv_page_indptr.cpu(); idx = kv_page_indices.cpu()
ok = True
for i in range(nnz_kv):
    b = int(bi[i]); pos = int(pi[i])
    go = int(indptr[b]) * page_size + pos
    page_iter, entry = go // page_size, go % page_size
    page_id = int(idx[page_iter])
    k_back = paged_kv_cache[page_id, 0, entry]   # [:, 0] 是 K
    v_back = paged_kv_cache[page_id, 1, entry]   # [:, 1] 是 V
    if not (torch.equal(k_back, k_append[i]) and torch.equal(v_back, v_append[i])):
        ok = False
        print(f"mismatch at token {i}")
print("append verified:", ok)
```

**操作步骤**：把上面脚本存为 `try_append.py`，在装好 flashinfer 的环境运行 `python try_append.py`。
**需要观察的现象**：
- 首次运行会有一次 JIT 编译（page 模块），后续运行直接命中缓存、启动很快；
- `batch_indices` 应为 `[0,0,0,0,0, 1,1,1]`，`positions` 应为 `[0,1,2,3,4, 0,1,2]`（请求 0 写到自己的第 0~4 个位置，请求 1 写到自己的第 0~2 个位置）。
**预期结果**：打印 `append verified: True`。
**待本地验证**：JIT 编译耗时与具体 GPU/驱动相关，本讲不预设数值；若机器无可用 CUDA 设备，可只做「源码阅读型实践」——跟踪 `batch_indices[i], positions[i]` 在 [page.cuh:L351-L358](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L351-L358) 里如何变成一次目标地址写入。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `append_paged_kv_cache` 适合放进 CUDA Graph，而 attention 的 `plan` 不适合？

**答案**：append kernel 只做「按固定映射 scatter 拷贝」，其计算图形状（grid、每个 token 的写地址）只依赖 `nnz` 与页表内容，而这些都以张量形式喂入、可在图捕获时固定，故可进图。而 `plan`（u3-l1 讲过）会根据批次结构做**动态决策**（split-k 划分、kernel 选择），产生依赖输入的元数据，形状/逻辑随批次变化，故不能进图。

**练习 2**：若把 `kv_layout` 从 `"NHD"` 改成 `"HND"`，上面的 5D cache 形状与读回索引各应怎么改？

**答案**：cache 形状改为 `[3, 2, num_kv_heads, page_size, head_dim]`（头与 page_size 换位）；读回时 k 在 `paged_kv_cache[page_id, 0, :, entry, :]`（`:` 是所有头）。本质就是头维 h 与 token 维 n 交换位置，对应 [page.cuh:L143-L144](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh#L143-L144) 里 stride_n/stride_h 的对调。

---

## 5. 综合实践

把三个模块串起来，完成一个「**模拟两步推理的 KV-Cache 增长**」小任务，强化对「页表随序列增长而扩展」的理解。

**任务**：
1. 初始时请求 0 序列为空（0 页）。第一步 prefill 4 个 token（恰好 1 个满页），第二步 decode 1 个 token（此时 5 个 token，需 2 页，最后一页 1 个）。
2. 你需要：
   - 为每一步**手动维护** `kv_indices` / `kv_indptr` / `kv_last_page_len`（模拟调度器分配页）；
   - 每步调用 `append_paged_kv_cache` 写入新 K/V；
   - 用 `get_seq_lens` 反推并打印每步后的序列长度，确认从 0 → 4 → 5；
   - 最后把整条序列的 K 读出来，验证顺序与你写入的 token 顺序一致（注意会跨越两个物理页）。

**提示**：
- 第一步后：`kv_indptr=[0,1]`，`kv_indices=[0]`，`kv_last_page_len=[4]`。
- 第二步 decode 1 个 token 需要新分配物理页 1：`kv_indptr=[0,2]`，`kv_indices=[0,1]`，`kv_last_page_len=[1]`（最后一页只有新 token 1 个）。
- 读回时按 `pos=0..4` 依次定位（会落到物理页 0 的 0~3 槽、再落到物理页 1 的 0 槽）。

**验收标准**：两步后 `get_seq_lens` 返回 `[5]`，且读回的 K 序列与两步写入的 K 拼接结果逐元素相等。这个任务直接对应真实推理服务里「prefill + 连续 decode」的核心循环——只是页表分配由你手动完成，让你看清 append「只负责写、不负责分配」的边界。

## 6. 本讲小结

- **布局（NHD/HND）**：KV-Cache 后三维有两种合法排布，差异仅是头维 h 与 token 维 n 换位；FlashInfer 用一个枚举值在运行期选用不同 stride，避免重复代码。`stride_page` 与布局无关，`stride_n/stride_h` 因布局而异。
- **页表三件套**：`kv_indices`（物理页号列表，页表本体）、`kv_indptr`（划出每请求的页段）、`kv_last_page_len`（每请求最后一页有效长度）。三者唯一确定每个请求的序列长度与每个 token 的物理位置。
- **定位链路**：`(batch, pos) → kv_indptr → page_iter → kv_indices → 物理页号 → stride → 元素偏移`，其中 `kv_indices` 那一次间接正是分页的本质。
- **append 的边界**：`append_paged_kv_cache` 假设空间已分配（页表已更新），只做按映射的 scatter 拷贝；正因如此它形状固定、可进 CUDA Graph。
- **代码分层**：Python 入口校验+拆包 → torch custom op → TVM-FFI → csrc launcher 组装 `paged_kv_t` → include kernel 用 `get_k_ptr/get_v_ptr` 完成页表查找与写入；布局抽象在 launcher 抽维度、在结构体算 stride 两处落地。
- **JIT 形态**：page 模块的 `gen_page_module` 无参数（dtype/head_dim 都在 C++ 运行期派发），是 `gen_*_module` 的最简范例。

## 7. 下一步学习建议

- **下一讲 u3-l3** 将进入「**读取侧**」：`BatchDecodeWithPagedKVCacheWrapper` 如何复用本讲的 `paged_kv_t` 与页表，在 decode 阶段对单 query 做注意力计算。你会看到同一套布局/页表数据结构如何被 attention kernel 消费。
- 若想提前看「连续两次 append 如何串成 decode 循环」，可结合本讲综合实践与 u3-l1 的 plan/run 模型对照阅读 [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) 中调用 `append_paged_kv_cache` 的位置。
- 对量化感兴趣可先扫一眼 `nvfp4_quantize_append_paged_kv_cache`（[flashinfer/page.py:L532-L655](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L532-L655)），它在同一页表机制上额外做了 FP4 量化写入，是 u5 低精度单元的前置。
- 建议继续阅读 [include/flashinfer/page.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/page.cuh) 中 `paged_kv_t` 的全部方法（`protective_get_k_ptr` 等），它们在后续 attention kernel 里会反复出现。
