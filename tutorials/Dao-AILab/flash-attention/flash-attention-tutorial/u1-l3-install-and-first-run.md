# 安装并第一次调用 FA4

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `pip` 安装 `flash-attn-4`（含 CUDA 13 与开发态两种方式），并理解它的依赖构成；
- 写出 `flash_attn_func(q, k, v, causal=True)` 的最小调用，知道返回值是 `(out, lse)` 而不是单个张量；
- 说清楚 FA4 对输入张量的硬性要求：布局 `(batch, seqlen, num_heads, head_dim)`、最后一维连续、`head_dim` 满足 16 字节对齐。

承接上一讲 [u1-l2](u1-l2-repo-structure.md)：我们已经知道 FA4 住在 `flash_attn/cute/`、是纯 Python + CuTeDSL、安装时**不**编译。本讲就把「装好 → 跑通 → 不踩输入约束的坑」这条路走完。

## 2. 前置知识

- **CuTeDSL / JIT 编译**：FA4 用 Python 描述 kernel，运行时才被 `nvidia-cutlass-dsl` 编译成 PTX/CUBIN。所以 `pip install` 很快（只是装纯 Python 代码和依赖），但**第一次**调用 `flash_attn_func` 会比较慢（要编译 kernel，之后走缓存）。
- **`torch.autograd.Function`**：PyTorch 里自定义可求导算子的标准基类。FA4 的 `flash_attn_func` 本质是 `FlashAttnFunc.apply(...)`，前向算 `O`、反向自动给 `dQ/dK/dV`。
- **16 字节对齐**：GPU 上 TMA/向量化访存通常要求一段内存的起始地址是 16 字节的整数倍。对 FA4 来说，这意味着「每个元素的字节数 × head_dim」要能整除 16（详见 4.3）。
- **HBM / SRAM**：上一讲提到的显存层级概念，本讲不展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/README.md](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/README.md) | FA4 子包的安装与用法说明，本讲「最小示例」的直接出处 |
| [flash_attn/cute/pyproject.toml](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml) | 子包元数据：包名、Python 版本、依赖、可选扩展（cu13/dev） |
| [flash_attn/cute/__init__.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py) | 对外导出 `flash_attn_func` 与 `flash_attn_varlen_func` |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | 公共 API 的真正实现：`flash_attn_func` 签名、布局/对齐校验 |
| [README.md](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md) | 仓库总 README，其中的 FA4 小节给出官方安装/调用方式 |

---

## 4. 核心概念与源码讲解

### 4.1 安装方式与依赖

#### 4.1.1 概念说明

FA4 的发行包叫 **`flash-attn-4`**，是一个**纯 Python**包。这一点和 FA2/FA3 截然不同：

- FA2/FA3：安装时要用 `nvcc` 把 C++/CUDA 编译成扩展，装一次可能要几分钟到十几分钟；
- FA4：安装只是把 `flash_attn/cute/` 下的 `.py` 文件和依赖装上，**kernel 的编译推迟到运行时第一次调用**（CuTeDSL JIT）。

所以 FA4 的安装命令很短，但你要做好「第一次 `flash_attn_func` 会慢」的心理预期——那是在编译 kernel，不是卡死。

#### 4.1.2 核心流程

三种典型安装方式：

1. **发行版安装（CUDA 12.x）**
   ```sh
   pip install flash-attn-4
   ```
2. **CUDA 13（如 B200）最佳性能**：加 `cu13` 扩展，它会拉取带 CUDA 13 后端的 cutlass-dsl，并从 PyTorch 的 cu130 索引装 torch：
   ```sh
   pip install "flash-attn-4[cu13]"
   ```
3. **开发态安装**（改源码用，从仓库根目录执行）：
   ```sh
   pip install -e "flash_attn/cute[dev]"        # CUDA 12.x
   pip install -e "flash_attn/cute[dev,cu13]"   # CUDA 13.x
   ```

安装成功后，导入路径是 `flash_attn.cute`（注意：是命名空间包 `flash_attn` 下的 `cute` 子包，与 FA2 共存，见 [u1-l2](u1-l2-repo-structure.md)）。

#### 4.1.3 源码精读

包名与 Python 版本要求写在子包的 `pyproject.toml` 里：

[flash_attn/cute/pyproject.toml:L5-L11](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L5-L11) — 声明包名 `flash-attn-4`、版本由 setuptools-scm 动态生成、要求 Python ≥ 3.10。

核心依赖列表（这就是你 `pip install` 时会一起拉下来的东西）：

[flash_attn/cute/pyproject.toml:L24-L32](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L24-L32) — 依赖项。对初学者最重要的是前两个：

- `nvidia-cutlass-dsl`：CuTeDSL 编译器，把 Python kernel 描述编译成 PTX/CUBIN，是 FA4 的「引擎」；
- `torch`：提供张量类型与 autograd（FA4 的算子是 `torch.autograd.Function`）。

其余几个是运行时/绑定胶水：`einops`（张量重排）、`apache-tvm-ffi`（编译后 kernel 的 FFI 绑定）、`torch-c-dlpack-ext`（torch 张量与 CUTLASS 之间的 DLPack 互转）、`quack-kernels`（提供 `make_fake_tensor` 等编译期辅助工具）。

可选扩展 `cu13` 与 `dev`：

[flash_attn/cute/pyproject.toml:L34-L40](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L34-L40) — `cu13` 切换到 CUDA 13 后端的 cutlass-dsl；`dev` 装上 `pytest / pytest-xdist / ruff`，本讲义后续的测试、Lint 都依赖它。

子包把自己注册成 `flash_attn.cute`：

[flash_attn/cute/pyproject.toml:L46-L48](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L46-L48) — `packages = ["flash_attn.cute"]`，且 `package-dir` 把 `flash_attn.cute` 映射到当前目录 `.`。这正是它能把代码放在仓库的 `flash_attn/cute/` 下、却以 `flash_attn.cute` 这个模块名安装的原因（配合 FA2 的命名空间包机制）。

官方安装说明（与子包 README 一致）：

[flash_attn/cute/README.md:L5-L15](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/README.md#L5-L15) — 普通安装与 `cu13` 扩展安装两条命令。

[README.md:L82-L92](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L82-L92) — 根 README 的 FA4 小节，同样给出 `pip install flash-attn-4` 与 `cu13` 建议。

#### 4.1.4 代码实践

**目标**：装好包，确认能导入并读到版本号。

**操作步骤**：

1. 在已装好 torch（CUDA 版）的环境中执行 `pip install flash-attn-4`。
2. 新建 `check_fa4.py`：
   ```python
   import flash_attn.cute as fa4
   print("module:", fa4.__file__)
   print("version:", fa4.__version__)
   from flash_attn.cute import flash_attn_func, flash_attn_varlen_func
   print("exports:", flash_attn_func.__module__)
   ```
3. 运行 `python check_fa4.py`。

**需要观察的现象**：`module` 路径应指向 `.../flash_attn/cute/__init__.py`；`flash_attn_func.__module__` 应为 `flash_attn.cute.interface`。

**预期结果**：导入成功、无编译发生（编译在第一次调用 kernel 时才触发）。`__version__` 由 [flash_attn/cute/__init__.py:L5-L8](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L5-L8) 通过读取已安装的 `fa4` 发行版元数据得到，读不到则回退字符串 `"0.0.0"`。若你看到 `0.0.0`，说明版本元数据未被识别（开发态 `-e` 安装时可能出现），不影响功能。具体版本号**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FA4 的 `pip install` 比 FA2 快得多？
**答案**：FA4 是纯 Python 包，安装时只拷贝 `.py` 与依赖，不调用 `nvcc`；kernel 的 CUDA 编译推迟到运行时由 CuTeDSL JIT 完成。FA2 在安装期就要编译 C++/CUDA 扩展。

**练习 2**：在 B200（CUDA 13）上，应该用哪条安装命令？为什么？
**答案**：`pip install "flash-attn-4[cu13]"`。`cu13` 扩展会拉取带 CUDA 13 后端的 `nvidia-cutlass-dsl[cu13]`，官方说这样能在 CUDA 13 上获得最佳性能。

---

### 4.2 `flash_attn_func` 最小示例

#### 4.2.1 概念说明

FA4 对外只暴露两个入口（见 [flash_attn/cute/__init__.py:L15-L18](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L15-L18) 的 `__all__`）：

- `flash_attn_func`：标准（等长）注意力；
- `flash_attn_varlen_func`：变长注意力（一个 batch 里序列长度不同，本讲不讲）。

最小调用只需要三个张量 `q, k, v` 加一个 `causal=True`：

```python
from flash_attn.cute import flash_attn_func
out = flash_attn_func(q, k, v, causal=True)   # 注意：返回的是元组，见下文
```

> **新手最容易踩的坑**：FA4 的 `flash_attn_func` **始终返回二元组 `(out, lse)`**，而不是单个 `out`。直接 `out = flash_attn_func(...)` 会让 `out` 变成那个元组，后续 `.shape` 就会报错。正确写法是 `out, lse = flash_attn_func(...)` 或 `out, _ = flash_attn_func(...)`。

#### 4.2.2 核心流程

一次 `flash_attn_func(q, k, v, causal=True)` 的内部链路：

```
flash_attn_func(...)                      # interface.py 的薄封装
   └─> FlashAttnFunc.apply(...)           # torch.autograd.Function 入口
         └─> _flash_attn_fwd(...)         # 真正的前向
               ├─ _get_device_arch()      # 读 GPU 架构（SM80/90/100/...）选 kernel 类
               ├─ 校验布局/对齐/head_dim
               ├─ 取/编译 CuTeDSL kernel（命中缓存则秒返回）
               └─ launch kernel → 写入 out、lse
   返回 (out, lse)
```

`out` 是注意力结果，形状与 `q` 的「序列/头」部分一致、最后一维是 `head_dim_v`（多数情况 `head_dim_v == head_dim`）；`lse` 是 **log-sum-exp**，形状 `(batch, num_heads, seqlen_q)`、`float32`，反向传播与 SplitKV 合并都要用到它（详见 [u2-l1](u2-l1-public-api.md)、[u4-l1](u4-l1-online-softmax.md)）。

#### 4.2.3 源码精读

导出关系（你 `import` 时实际拿到的就是这两个函数）：

[flash_attn/cute/__init__.py:L10-L13](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L10-L13) — 从 `.interface` 把 `flash_attn_func`、`flash_attn_varlen_func` 引入子包命名空间。

`flash_attn_func` 的完整签名（参数很多，但**只有 `q/k/v` 是必填**，其余都有默认值）：

[flash_attn/cute/interface.py:L2709-L2731](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2731) — 签名。常用项：`softmax_scale`（缺省 `1/√head_dim`）、`causal`、`window_size`、`softcap`、`num_splits`、`pack_gqa`、`score_mod` / `mask_mod`（自定义打分/掩码回调）、`return_lse`。本讲只用到 `causal`，其余在后续讲义展开。

返回值确认（始终是元组）：

[flash_attn/cute/interface.py:L2488](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2488) — `FlashAttnFunc.forward` 末尾 `return out, lse`。这也是测试代码里统一写成 `out, lse = flash_attn_func(...)` 的原因。

官方最小用法：

[flash_attn/cute/README.md:L19-L23](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/README.md#L19-L23) — `from flash_attn.cute import flash_attn_func, flash_attn_varlen_func`，然后 `out = flash_attn_func(q, k, v, causal=True)`。（README 这里省略了 `lse`，实际请按元组解包。）

#### 4.2.4 代码实践

**目标**：跑通最小调用，打印输出的形状与 dtype，并和 PyTorch 参考注意力对比最大误差。

**操作步骤**：

1. 构造 fp16、布局 `(batch, seqlen, nheads, head_dim)` 的 `q/k/v`：
   ```python
   import torch
   from flash_attn.cute import flash_attn_func

   torch.manual_seed(0)
   batch, seqlen, nheads, hdim = 2, 512, 8, 64
   q = torch.randn(batch, seqlen, nheads, hdim, dtype=torch.float16, device="cuda")
   k = torch.randn_like(q)
   v = torch.randn_like(q)
   ```
2. 调用 FA4（**第一次会触发 JIT 编译，较慢；后续命中缓存就快**）：
   ```python
   out, lse = flash_attn_func(q, k, v, causal=True)
   print(out.shape, out.dtype)        # 预期 torch.Size([2, 512, 8, 64]) torch.float16
   print(lse.shape, lse.dtype)        # 预期 torch.Size([2, 8, 512]) torch.float32
   ```
3. 写一个带因果掩码的 PyTorch 参考实现并对比：
   ```python
   def attn_ref(q, k, v, causal=True):
       # 转成 (batch, nheads, seqlen, hdim) 方便批量矩阵乘
       q = q.transpose(1, 2).float()
       k = k.transpose(1, 2).float()
       v = v.transpose(1, 2).float()
       scores = torch.matmul(q, k.transpose(-2, -1)) / (q.shape[-1] ** 0.5)
       if causal:
           mask = torch.triu(torch.ones(seqlen, seqlen, dtype=torch.bool), diagonal=1)
           scores = scores.masked_fill(mask, float("-inf"))
       out = torch.softmax(scores, dim=-1) @ v
       return out.transpose(1, 2).to(torch.float16)

   ref = attn_ref(q, k, v, causal=True)
   print("max abs err:", (out - ref).abs().max().item())
   ```

**需要观察的现象**：`out.shape == (2, 512, 8, 64)`、`lse.shape == (2, 8, 512)`；与参考实现的最大绝对误差是 fp16 量级的小数。

**预期结果**：误差通常在 `1e-2 ~ 1e-1` 量级（fp16 精度下属正常）。具体数值**待本地验证**。FA4 是**精确**注意力（不是近似），误差完全来自 fp16 舍入，而非算法本身（见 [u1-l1](u1-l1-what-is-flashattention.md)）。

#### 4.2.5 小练习与答案

**练习 1**：下列代码运行后 `type(out)` 是什么？为什么？
```python
out = flash_attn_func(q, k, v, causal=True)
print(out.shape)
```
**答案**：`out` 实际是元组 `(out_tensor, lse)`，`type(out)` 是 `tuple`，`out.shape` 会抛 `AttributeError`。因为 `FlashAttnFunc.forward` 在 [interface.py:L2488](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2488) `return out, lse`。应改为 `out, lse = flash_attn_func(...)`。

**练习 2**：为什么第一次调用慢、第二次快？
**答案**：第一次调用时 CuTeDSL 把 kernel JIT 编译成 PTX/CUBIN 并写缓存；第二次命中缓存直接加载已编译产物。编译缓存机制详见 [u11-l1](u11-l1-jit-and-cache.md)。

---

### 4.3 张量布局与对齐约束

#### 4.3.1 概念说明

FA4 对输入张量有三条硬性要求，违反要么被静默 `.contiguous()`，要么直接断言报错：

1. **布局**：`q/k/v` 都是 `(batch, seqlen, nheads, head_dim)`，即「头维度 `head_dim` 在最后一维」。`k/v` 的头数可以少于 `q`（即 GQA/MQA，见 [u7-l1](u7-l1-pack-gqa.md)），但 `q` 的头数必须能被 `k/v` 的头数整除。
2. **最后一维连续**：`stride(-1) == 1`。不满足时代码会自动帮你 `.contiguous()`（所以非最后维的 stride 可以任意，但最后维必须紧挨）。
3. **16 字节对齐**：等价于要求 `head_dim` 是 `16 // element_size` 的整数倍——fp16/bf16 是 2 字节，所以 `head_dim` 必须是 **8 的倍数**。

> 第三条是初学者最容易忽略的：`head_dim=64` 没问题（64 % 8 == 0），但若你随手造一个 `head_dim=70` 的张量，就会在 `_validate_head_dims` 处直接 `AssertionError`。

#### 4.3.2 核心流程

FA4 在前向入口对输入做的三步处理：

```
1. maybe_contiguous(t)：若 t.stride(-1) != 1，调用 t.contiguous()
   → 保证「最后一维连续」
2. alignment = 16 // v.element_size()
   → fp16/bf16 得 8，fp32 得 4
3. _validate_head_dims(head_dim, head_dim_v, arch, alignment)
   → 要求 head_dim % alignment == 0，否则 AssertionError
```

`softmax_scale` 缺省时按 `1/√head_dim` 计算（标准缩放），LSE 的形状按是否变长分两种情况。

#### 4.3.3 源码精读

最后一维连续的自动修复：

[flash_attn/cute/interface.py:L240-L241](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L240-L241) — `maybe_contiguous`：只有当 `stride(-1) != 1`（最后一维不连续）时才触发 `.contiguous()`，避免无谓的拷贝。前向里 `q, k, v, qv` 都会过这一遍。

16 字节对齐的推导：

[flash_attn/cute/interface.py:L449-L451](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L449-L451) — `alignment = 16 // v.element_size()`，随后调用 `_validate_head_dims`。语义：每个 head 的字节数 = `head_dim × element_size`，要能整除 16 字节，等价于 `head_dim` 整除 `16 // element_size`。

头维合法性校验（不同架构支持的 `head_dim` 范围不同）：

[flash_attn/cute/interface.py:L95-L112](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L95-L112) — SM90 允许 `8 ≤ head_dim ≤ 256`；SM100/SM110 默认只允许 `8 ≤ head_dim ≤ 128`，外加 DeepSeek 形状 `(192,128)`、MLA absorbed `(64,512)`、专用 `(256,256)`。所有情况都要求 `head_dim % alignment == 0`。

缺省缩放因子：

[flash_attn/cute/interface.py:L452-L456](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L452-L456) — `softmax_scale` 为 `None` 时取 `1/√head_dim`（标准 SDPA 缩放）。所以 `head_dim=64` 时 `softmax_scale = 1/8`。

LSE 的形状：

[flash_attn/cute/interface.py:L471-L475](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L471-L475) — 非变长、无 `qv` 时 `lse_shape = (batch_size, num_head, seqlen_q)`，`float32`。

#### 4.3.4 代码实践

**目标**：亲手触发一次对齐校验失败，理解报错来源；并验证「最后一维不连续会被自动修复」。

**操作步骤**：

1. **触发对齐报错**（`head_dim=70` 不被 8 整除）：
   ```python
   q = torch.randn(1, 128, 8, 70, dtype=torch.float16, device="cuda")
   k = torch.randn_like(q)
   v = torch.randn_like(q)
   try:
       flash_attn_func(q, k, v)
   except AssertionError as e:
       print("AssertionError:", e)
   ```
2. **验证 maybe_contiguous**：构造一个最后一维不连续的张量，确认调用不会因此报错：
   ```python
   q = torch.randn(1, 128, 8, 64, dtype=torch.float16, device="cuda")
   q_t = q.transpose(1, 2)          # (1, 8, 128, 64)，最后一维仍连续
   print("stride(-1):", q_t.stride(-1))
   out, _ = flash_attn_func(q_t, q_t, q_t, causal=True)
   print(out.shape)
   ```

**需要观察的现象**：第 1 步应抛出 `AssertionError`，信息提到 `head_dim ... divisible by 8`（来自 [interface.py:L104-L106](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L104-L106) 或对应的 SM100 分支）；第 2 步 `stride(-1)` 为 1、调用正常返回 `(1, 128, 8, 64)`。

**预期结果**：第 1 步报错、第 2 步成功。若第 2 步你故意把最后一维做成不连续（如对 `(..., 64, 8)` 做转置使 `stride(-1) != 1`），`maybe_contiguous` 会静默 `.contiguous()`，仍能跑通但多一次拷贝。具体报错文案**待本地验证**（取决于你的 GPU 架构走哪个校验分支）。

#### 4.3.5 小练习与答案

**练习 1**：fp32 输入时，`head_dim` 必须是多少的倍数？为什么？
**答案**：4 的倍数。因为 `alignment = 16 // element_size = 16 // 4 = 4`，[interface.py:L449](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L449)。注意 FA4 主力是 fp16/bf16，fp32 通常只用于参考实现或调试。

**练习 2**：如果你的 `q` 形状是 `(batch, nheads, seqlen, head_dim)`（头维度在第 2 维），直接传给 `flash_attn_func` 会怎样？
**答案**：FA4 **不会**自动重排维度，它按 `(batch, seqlen, nheads, head_dim)` 解释。形状错位要么导致结果完全错误，要么因 stride/形状不匹配在内部断言失败。正确做法是先 `q.transpose(1, 2)` 把头维度换到第 2 维、使最后一维是 `head_dim`，并确保 `.contiguous()`。

---

## 5. 综合实践

把本讲的三块内容（安装验证 + 最小调用 + 输入约束）串成一个自检脚本。这个脚本可以当作你日后在新环境里「确认 FA4 可用」的标准动作。

**任务**：编写 `fa4_smoke.py`，完成以下四件事并用 `print` 输出每步结论：

1. 打印 `flash_attn.cute.__version__` 与 `flash_attn_func.__module__`，确认安装与导入正确（对应 4.1）。
2. 构造合法输入 `(2, 512, 8, 64)` fp16，调用 `flash_attn_func(q, k, v, causal=True)`，正确解包 `(out, lse)`，打印二者形状与 dtype（对应 4.2）。
3. 用 4.2.4 里的参考实现计算 `ref`，打印 `max abs err`（对应 4.2）。
4. 构造一个 `head_dim=70` 的张量调用 `flash_attn_func`，捕获 `AssertionError` 并打印信息，验证 16 字节对齐约束（对应 4.3）。

**验收标准**：第 2 步输出 `(2,512,8,64)` 与 `(2,8,512)`；第 3 步误差在 fp16 量级；第 4 步捕获到断言错误。完成后再回头读一遍 4.3.3 的三处源码，确认你能把「报错信息 ↔ 对应源码行」一一对应。

> 提示：第一次运行第 2 步会触发 JIT 编译（数十秒），属正常现象。如想分别度量「编译耗时」与「执行耗时」，可在第 2 步前后再调用一次并对比时间。

## 6. 本讲小结

- FA4 发行包名是 `flash-attn-4`，纯 Python，`pip install` 不编译；kernel 在运行时由 CuTeDSL JIT 编译，所以**第一次调用慢、之后命中缓存就快**。CUDA 13 用 `cu13` 扩展、改源码用 `-e ...[dev]`。
- 对外入口只有 `flash_attn_func` 和 `flash_attn_varlen_func`，都从 `flash_attn.cute` 导入；最小调用只需 `q/k/v` 加 `causal=True`。
- **关键坑**：`flash_attn_func` 始终返回元组 `(out, lse)`，要用 `out, lse = ...` 解包；`lse` 形状 `(batch, num_heads, seqlen_q)`、`float32`。
- 输入布局固定为 `(batch, seqlen, nheads, head_dim)`；最后一维不连续会被 `maybe_contiguous` 静默修复；`head_dim` 必须满足 16 字节对齐（fp16/bf16 即 8 的倍数），否则 `_validate_head_dims` 抛 `AssertionError`。
- `softmax_scale` 缺省为 `1/√head_dim`；FA4 是**精确**注意力，与参考实现的误差仅来自 fp16 舍入。

## 7. 下一步学习建议

- 想系统了解 `flash_attn_func` 每个参数（`window_size`、`softcap`、`num_splits`、`score_mod`、`mask_mod`、`pack_gqa` 等）的语义与返回的 `lse` 怎么用，请进入 [u2-l1 公共 API 详解](u2-l1-public-api.md)。
- 想知道 FA4 如何根据你的 GPU（SM80/90/100/...）自动选择不同 kernel，以及 `tile` 尺寸是怎么决定的，请看 [u2-l2 架构分发与 tile 配置选择](u2-l2-arch-dispatch-and-config.md)。
- 对「为什么返回 `lse`、它在数学上代表什么」感兴趣，可在学完 [u4-l1 在线 Softmax 数值核心](u4-l1-online-softmax.md) 后再回看本讲的 LSE 形状，会有更深的理解。
