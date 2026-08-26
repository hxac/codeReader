# 正确性测试体系：Python 端到端与独立 CUDA 测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 BitDecoding 的两层正确性验证分别在哪一层运行、各自对照什么参考实现、判定阈值是多少。
2. 读懂 `single_mha` 这个用 `einsum + softmax` 写成的 FP16 参考实现，并解释它为什么是验证低比特 kernel 的「金标准」。
3. 逐段精读 `TestDecodingKernelCorrectness`（residual 版），理解它如何在纯 C++ 环境里复现「打包 → 残余 → decode → 攒满回写 → 再 decode」的完整闭环。
4. 独立操作 `csrc/bit_decode/CMakeLists.txt`：恢复被注释的测试 target、修正 cutlass include 路径、用 CMake 编译运行 kernel 级测试；当编译失败时，能写出有证据的错误分析报告。
5. 建立分层验证思路：kernel 级（.cu 测试）→ Python API 级（`evaluation/test.py`）→ 模型级（example.py 生成质量），知道每层能抓住什么类型的 bug。

## 2. 前置知识

### 2.1 为什么要分层测试

BitDecoding 的正确性风险来自三个层次的叠加：

| 层次 | 被测对象 | 参考实现 | 本讲对应文件 |
|---|---|---|---|
| kernel 级 | CUDA kernel 本体（qpack / splitkv / residual / combine） | `single_mha`（einsum+softmax） | `csrc/bit_decode/src/test_*.cu` |
| Python API 级 | pybind 绑定 + Python 接口 + DynamicCache | `attention_ref`（同样是 einsum+softmax） | `evaluation/test.py` |
| 模型级 | HF 模型端到端生成 | 生成文本的质量 / 与 FP16 后端对比 | `evaluation/example.py`（第 6 单元已讲） |

分层的原因是**定位半径**：如果模型级生成出了乱码，你不知道是注意力算错、缓存拼错还是模型接入错了；如果 kernel 级测试单独通过而模型级失败，问题大概率在 Python 胶水层（缓存拼接、参数传递）。

### 2.2 误差指标：MAE、MSE 与最大误差

低比特量化是有损压缩，所以**不能用 `assert == 0`**，只能统计输出与 FP16 参考的偏差：

\[ \text{MAE} = \frac{1}{N}\sum_{i=1}^{N} |out_i - ref_i|, \qquad \text{MSE} = \frac{1}{N}\sum_{i=1}^{N} (out_i - ref_i)^2 \]

- **MAE**（平均绝对误差）衡量整体偏差水平，对离群值不敏感；
- **MSE** 放大离群值，一个大错会被平方放大；
- **max error** 回答「最坏情况错到什么程度」，工程上往往比均值更有信息量。

三个 .cu 测试中，residual 与 single_packdecode 的通过阈值是 MAE < 0.1 且 MSE < 0.1（很宽松，因为量化误差天然存在），而 batch_packdecode 用 0.01（更严）。`evaluation/test.py` 则干脆不做判定，只打印每轮 MAE 供人眼观察。

### 2.3 einsum 写法的参考注意力

标准多头注意力的数学定义：

\[ \mathrm{Out} = \mathrm{softmax}\left(\frac{Q K^\top}{\sqrt{d}}\right) V \]

对形状 `(b, s, h, d)` 的张量，`torch.einsum("bthd,bshd->bhts", q, k)` 一步完成「按头内积」，得到每头每行每列的分数矩阵，不需要手工 transpose/reshape——这正是参考实现选择 einsum 的原因：**写法与公式一一对应，参考实现本身几乎不可能写错**。

### 2.4 模板实例化与链接：为什么测试要链 5 个 genfile 文件

第三、四、五单元讲过：kernel 以 `<Headdim, quant_mode, num_bits, group_size>` 为模板参数，运行期 dispatch 只是一个 if 链，真正可执行代码由 `genfile/*.cu` 显式实例化。所以任何绕过 pip 扩展、直接使用 kernel 的 C++ 程序，都必须**把 5 个 genfile 一起链进来**，否则链接器报 undefined reference。CMake target 的源文件列表正是这个「实例化集合」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | Python 层正确性测试：打包 + 32 轮 decode，逐轮打印与 `attention_ref` 的 MAE |
| [csrc/bit_decode/src/test_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu) | **唯一与当前 API 签名同步**的 kernel 级测试：两轮闭环 + 17 个 seqlen 扫描点，CMake 中唯一启用的测试 target |
| [csrc/bit_decode/src/test_single_packdecode.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_packdecode.cu) | 单 batch 纯打包解码测试，**调用旧版 API 签名，当前无法编译**，target 被注释 |
| [csrc/bit_decode/src/test_batch_packdecode.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_batch_packdecode.cu) | paged-KV（block_table）批量测试，**同样调用旧版签名**，target 被注释 |
| [csrc/bit_decode/CMakeLists.txt](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt) | 独立 CMake 构建：定义 test/bench target，include 路径硬编码（有坑） |
| [csrc/bit_decode/src/flash_api.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h) | `mha_fwd_kvcache` / `kvcache_qpack` 的当前签名与 dispatch（判断测试是否过期的依据） |
| [setup.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py) | pip 扩展的构建配置，其 include 路径写法是修复 CMake 的参照 |

## 4. 核心概念与源码讲解

### 4.1 Python 端正确性基线：attention_ref 与 32 轮 decode 回归

#### 4.1.1 概念说明

`evaluation/test.py` 是最轻量的验证入口：**不加载任何大模型**，随机生成一个 batch=1、seqlen_k=1024 的 K/V，走完整的「prefill 打包 + 逐轮 decode」流程，每轮与 FP16 参考注意力比对。它验证的是从 Python 接口到 CUDA kernel 的整条链路，但**不判定通过与否**——只打印数值，让人眼判断误差量级是否合理（4-bit 应在 1e-2 量级，2-bit 显著更大）。

#### 4.1.2 核心流程

```text
配置: k-channel / 4bit / group_size=32 / residual_block_size=128
Round 1 (prefill):
  随机生成 q(1,1,32,128), k_state/v_state(1,1024,32,128)
  residual_len = 1024 % 128 = 0 → 无残余，全部 1024 个 token 打包
  kvcache_pack_int(...) → update_pack 写入 DynamicCache
Round 2-33 (decode, 32 轮):
  每轮:
    update_pack(None,...) 读回主缓存
    新 kv 追加进残余区, 补零对齐到 128
    fwd_kvcache_int(...) → out_bitdecode + 4 个 *_new
    若 cur_residual_len == 128: update_pack(*_new) + clear_residual
    k_state/v_state 拼上新 token → attention_ref 算 FP16 参考
    打印 (out_bitdecode - out_ref).abs().mean()
```

#### 4.1.3 源码精读

参考实现 `attention_ref`，先对 Q 除以 \(\sqrt{d}\) 再做 einsum 打分、softmax 后与 V 加权：

- [evaluation/test.py:13-37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L13-L37)：`attention_ref` 完整定义，docstring 标清了四个张量的形状约定；
- [evaluation/test.py:31](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L31)：`scores = einsum("bthd,bshd->bhts", q / math.sqrt(d), k)`——缩放融合在 Q 上；
- [evaluation/test.py:35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L35)：`output = einsum("bhts,bshd->bthd", attention, v)`——第二个 einsum 完成 softmax 权重对 V 的加权求和。

测试配置就是本讲实践任务要求的 k-channel / 4bit / group_size=32：

- [evaluation/test.py:40-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L45)：量化参数区（`quant_mode="k-channel"`、`num_bits=4`、`group_size=32`、`residual_block_size=128`）；
- [evaluation/test.py:68-70](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L68-L70)：`residual_len = seqlen_k % residual_block_size`——**注意：余 0 时残余区为空**，这与下面 4.3 的 .cu 测试规则不同；
- [evaluation/test.py:78-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82)：按第二单元的布局分配 `k_pack/k_params/v_pack/v_params` 四个张量；
- [evaluation/test.py:97-107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L97-L107)：调用 `kvcache_pack_int` 量化后 `update_pack` 存入缓存。

decode 循环的关键四步：

- [evaluation/test.py:121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L121)：`update_pack(None, None, None, None, layer_idx)`——全 None 时它退化为**主缓存读取器**（第 6 单元讲过）；
- [evaluation/test.py:127-135](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L127-L135)：新 token 经 `update_residual` 追加进残余缓存，再拷进补零对齐的固定形状缓冲；
- [evaluation/test.py:137-150](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L137-L150)：`fwd_kvcache_int` 返回 5 元组（注意力输出 + 4 个 `*_new`）；
- [evaluation/test.py:152-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L160)：`cur_residual_len == residual_block_size` 时拼回主缓存并清空残余；随后与逐轮增长的 FP16 `k_state/v_state` 比对，打印 MAE。

注意一个时序事实：初始残余为 0，每轮追加 1 个 token，32 轮后 `cur_residual_len` 最多到 32，**默认 32 轮内永远不会触发第 152 行的拼回分支**。要看到 `update_pack(*_new)` 触发，需把循环次数加到 128 轮以上。

#### 4.1.4 代码实践

**实践目标**：拿到 k-channel / 4bit / group_size=32 配置的 Python 级误差基线，并与 2-bit 对比。

1. 打开 `evaluation/test.py`，确认第 41-45 行配置为 `k-channel / 4 / 32 / 128`；
2. 在有 GPU 的机器上运行 `python evaluation/test.py`；
3. 记录 32 轮的 MAE 打印值（格式为 `Round N: bitdecode vs pytorch: ...`）；
4. 把第 42 行改成 `num_bits = 2`（第 43 行 `pack_nums` 与第 45 行 `residual_block_size` 会自动随之变为 8 与 256 的语义，但注意第 45 行是手写常量 128，**需要手动改成 256**），重新运行；
5. 对比两组 MAE 的数量级。

**需要观察的现象**：4-bit 时 MAE 应稳定在 1e-2 量级；2-bit 时显著增大（可能到 1e-1 量级）；每轮误差随 `cur_residual_len` 增长没有系统性发散（残余区保住了新 token 的精度）。

**预期结果**：两组数值均非零——这是量化的固有代价，不是 bug。若出现 NaN 或 >1 的误差，说明链路有真实错误。

**待本地验证**：具体数值需在 GPU 机器上运行确认；无 GPU 时可通读循环并写出每步张量形状变化表代替。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `attention_ref` 的参考输出也用 FP16 的 `k_state/v_state`（而非量化后的值），比较结果却非零？

**答案**：被测路径把历史 K/V 量化成了 int4 打包存储，`fwd_kvcache_int` 输出的是「反量化近似值」参与的注意力；参考路径用原始 FP16 值。两条路径的数据本身就不同，差值就是量化误差的体现。

**练习 2**：把 `seqlen_k` 从 1024 改成 1000，prefill 阶段会发生什么变化？

**答案**：`residual_len = 1000 % 128 = 8`（第 68-70 行），于是 `residual=True`：前 992 个 token 走 `kvcache_pack_int` 打包，最后 8 个 token 经 `update_residual` 进入 FP16 残余区（第 87-92 行的 if 分支）。

**练习 3**：`test.py` 为什么不写 `assert MAE < 0.1` 这样的判定？

**答案**：可接受的误差量级取决于 num_bits、group_size 与应用容忍度，作者选择只打印不判定；判定逻辑放在 .cu 测试里（阈值 0.1 / 0.01），两层的「测试哲学」不同：Python 层做回归观察，kernel 层做硬判定。

### 4.2 single_mha：三个 CUDA 测试共用的参考实现

#### 4.2.1 概念说明

三个 .cu 测试文件的开头都有一份逐字相同的 `single_mha` 函数。它就是 C++ 版的 `attention_ref`：用 ATen（PyTorch C++ API）的 einsum + softmax 写成，充当 kernel 输出的「金标准」。把它放进测试文件本身，而不是某个公共头文件，是为了让每个测试**自包含**——复制走任何一个 .cu 都能独立编译。

#### 4.2.2 核心流程

```text
输入 q, k, v (形状 b×s×h×d，FP16)
1. scaled_q = q × (1/√d)
2. scores   = einsum("bthd,bshd->bhts", scaled_q, k)
3. attention= softmax(scores, dim=-1) → 转成 v 的 dtype
4. output   = einsum("bhts,bshd->bthd", attention, v)
返回 output（形状 b×s_q×h×d）
```

#### 4.2.3 源码精读

- [csrc/bit_decode/src/test_single_residual.cu:5-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L5-L14)：`single_mha` 定义。第 7 行算 `sm_scale = 1/√head_dim`，第 10-12 行三个 ATen 调用完成整个注意力；
- 同一函数的复制版在 [csrc/bit_decode/src/test_single_packdecode.cu:5-13](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_packdecode.cu#L5-L13) 与 [csrc/bit_decode/src/test_batch_packdecode.cu:5-13](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_batch_packdecode.cu#L5-L13)。

两个值得注意的细节：

1. **缩放位置一致**：`single_mha` 把 \(1/\sqrt{d}\) 乘在 Q 上（第 7-8 行），与 `test.py:31` 的 `q / math.sqrt(d)` 数学等价，也与 kernel 内 `scale_apply_exp2` 用 \(\log_2 e / \sqrt{d}\) 的 exp2 形式等价（第五单元）。
2. **运行设备跟随输入**：`single_mha` 不搬移张量。residual 测试在**CPU** 上算参考（传入 `Q_host` 等 host 张量，kernel 输出先 `.to(torch::kCPU)`）；而 batch 测试在第 152 行直接传入 `Q_device`，参考在 **GPU** 上算。前者是跨设备验证（更独立），后者更快。

#### 4.2.4 代码实践

**实践目标**：确认参考实现与手写公式逐项对应。

1. 对照 [test_single_residual.cu:5-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L5-L14) 的四行核心代码，在纸上写出每步输出形状：设 `b=1, s_q=1, s_k=991, h=32, d=128`；
2. 用一段 10 行以内的独立 PyTorch 代码（示例代码，非项目原有）复现同样计算并与 `evaluation/test.py` 的 `attention_ref` 输出比对：

```python
# 示例代码：验证 single_mha 与 attention_ref 等价
import torch, math
q = torch.randn(1, 1, 32, 128, dtype=torch.float16)
k = torch.randn(1, 991, 32, 128, dtype=torch.float16)
v = torch.randn(1, 991, 32, 128, dtype=torch.float16)
d = 128
scores = torch.einsum("bthd,bshd->bhts", q / math.sqrt(d), k)   # (1,32,1,991)
out = torch.einsum("bhts,bshd->bthd", torch.softmax(scores, -1).half(), v)
print(out.shape)  # 期望 (1, 1, 32, 128)
```

**需要观察的现象**：scores 形状为 `(1, 32, 1, 991)`（b,h,t,s 四维），输出回到 `(1, 1, 32, 128)`。

**预期结果**：两种写法输出完全一致（可加一行 `(a - b).abs().max()` 验证为 0）。此实践 CPU 即可运行。

#### 4.2.5 小练习与答案

**练习 1**：`single_mha` 为什么要在 softmax 后 `.to(v.dtype())`？

**答案**：ATen 的 softmax 对 FP16 输入通常返回 FP16，但内部累加可能走 float；显式转成 `v.dtype()` 保证参考实现与 kernel 的 dtype 约定一致（attention 权重 FP16、与 V 的加权求和也在 FP16 下进行），避免参考实现自己引入意外的精度差。

**练习 2**：einsum 字符串 `"bthd,bshd->bhts"` 里，哪两个下标做了内积？

**答案**：`d`（head_dim）。`b`、`h` 是逐批逐头的并行维，`t` 与 `s` 分别保留为输出的行与列——这正是打分矩阵 \(QK^\top\) 的按头分块形式。

### 4.3 TestDecodingKernelCorrectness：residual 版两轮全链路测试

#### 4.3.1 概念说明

[test_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu) 里的 `TestDecodingKernelCorrectness` 是当前仓库**唯一与最新 API 签名同步、且在 CMake 中启用**的 kernel 级测试。它的价值在于：不经过 Python、不经过 pybind，在纯 C++ 环境里直接调用 `kvcache_qpack` 与 `mha_fwd_kvcache`，覆盖两个 round——Round 0 验证「打包区 + FP16 残余区」的注意力，Round 1 验证「残余攒满 → 回写主缓存 → 残余清零重填」的闭环，也就是第 5 单元讲的 residual kernel 原位再量化路径。

#### 4.3.2 核心流程

```text
TestDecodingKernelCorrectness<num_heads=32, num_heads_kv=32, head_dim=128, num_bits=4>(bs, seqlen_kv, "k-channel", group_size):
  Round 0:
    残余切分规则: r = seqlen_kv % 128; r==0 则 r=128   ← 与 test.py 不同！
    seqlen_kv ← seqlen_kv - r （打包区长度，整除 128）
    随机生成 Q/K/V；按布局分配 4+4 个张量（含 *_new 四件套）
    K,V reshape 折叠 batch 维 → kvcache_qpack<4> 量化
    构造 (bs, 128, h, d) 补零残余缓冲，前 r 个填新随机 token，new_lens=r
    mha_fwd_kvcache<4>(...) → 5 元组
    CPU 上 single_mha(打包区 K + 残余 K 拼接) 算参考
    打印 MAE/MSE/max_error，阈值 0.1 判定
  Round 1 (追加 1 个 token):
    若 Round 0 的 new_lens==128（攒满）:
      torch.cat 把 *_new 拼回主缓存（k_pack dim=1, v_params dim=-1）
      残余缓冲清零、只填这 1 个新 token
    否则: 新 token 追加在残余缓冲第 new_lens 个位置
    重算 r、new_lens → 第二次 mha_fwd_kvcache → 再比对
  main(): 对 17 个精心选择的 seqlen 扫描点重复上述两轮
```

#### 4.3.3 源码精读

**模板与随机种子**。函数是四参模板，`num_bits` 是编译期常量：

- [test_single_residual.cu:16-19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L16-L19)：模板签名与 `torch::manual_seed(42)`——固定种子保证每次运行生成同样的数据，失败可复现。

**残余切分规则（与 test.py 的差异点）**：

- [test_single_residual.cu:21-25](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L21-L25)：`residual_block_size = num_bits == 4 ? 128 : 256`；第 24 行的三目运算写成一行是——若 `seqlen_kv % residual_block_size == 0` 则**保留整整一块**（128）作为残余，否则取余数。这与 `test.py:68` 的「余 0 → 残余为空」不同：.cu 测试刻意保证残余区永远非空，因为它的目的就是压测 residual kernel；同时保证打包区长度恒被 128 整除（进而被 pack_nums=4 与 group_size 整除），张量形状才是整数。

**张量分配**：

- [test_single_residual.cu:38-54](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L38-L54)：`if (quant_mode == "k-channel")` 分支按第二单元的 k-channel 布局分配 `k_pack/k_params` 及 `*_new` 四件套；`else` 分支是 k-tensor 布局的形状（虽然 dispatch 当前未启用该模式，测试代码先行保留了形状逻辑）。V 的 `v_pack/v_params` 两种模式共用，恒为 tensor 布局。

**折叠 batch 并调用量化**：

- [test_single_residual.cu:57-60](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L57-L60)：`reshape({bs * seqlen_kv, ...})` 把 (b,s,h,d) 折成 (b·s,h,d)——第三单元讲过，qpack 约定输入折叠 batch 维，batch 数由 `cu_seqlens_k.numel()-1` 恢复；
- [test_single_residual.cu:65-73](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L65-L73)：调用 `kvcache_qpack<num_bits>` 完成量化打包，签名与 [flash_api.h:602-614](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L602-L614) 一致（这部分与当前代码同步）。

**残余缓冲的补零对齐**：

- [test_single_residual.cu:80-93](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L80-L93)：分配 `(bs, residual_block_size, h, d)` 的全零缓冲，`slice(1, 0, residual_len).copy_(K_new_host)` 只填前 r 个有效 token，`new_lens = r` 告诉 kernel 有效长度——与 `test.py:127-135`、`llama.py` decode 分支的做法完全同构。

**被测调用（新签名）**：

- [test_single_residual.cu:101-112](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L101-L112)：结构化绑定接收 `mha_fwd_kvcache<num_bits>` 返回的 5 元组 `(out, k_pack_new_1, k_params_new_1, v_pack_new_1, v_params_new_1)`，实参表依次是 Q、四个打包缓存、三个 optional（残余 K/V 与 seqlens_k）、四个 `*_new` 输出缓冲、block_table、`sm_scale/quant_mode/group_size/residual_block_size/new_lens`——与 [flash_api.h:313-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L313-L341) 的当前签名逐位对齐。这就是「该测试能编译」的硬证据。

**参考比对与阈值**：

- [test_single_residual.cu:117-128](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L117-L128)：参考输入是「打包区 K/V + 残余新 token」在序列维 `torch.cat` 后的完整 FP16 序列；误差三项 MAE/MSE/max_error 全算；
- [test_single_residual.cu:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L130-L137)：判定条件 `mean_absolute_error < 1e-1 && mean_squared_error < 1e-1`，通过打印 `test pass !` 与三项误差值。

**一个阅读陷阱**：第 139-148 行的打印标签写的是 `out_cpu[0,0,0,:]`，但第 140 行实际访问的是 `out_cpu.index({0,0,1})`——**打印的是第 1 号头（第二个头），不是第 0 号头**（见 [test_single_residual.cu:139-148](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L139-L148)）。对比两组数字时以 index 为准，不要被标签误导；Round 1 的打印（第 226 行）则确实是 `{0,0,0}`。

**Round 1：攒满回写闭环**（本测试的灵魂）：

- [test_single_residual.cu:162-172](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L162-L172)：若 Round 0 的 `new_lens == residual_block_size`，用四个 `torch.cat` 把 kernel 产出的 `*_new` 拼进主缓存——`k_pack/k_params/v_pack` 沿 `dim=1`，唯独 `v_params` 沿 `dim=-1`。这四行正是 Python 侧 `DynamicCache.update_pack` 拼接维度的 C++ 镜像（第 6 单元）；随后残余缓冲清零、只放入 Round 1 的 1 个新 token；
- [test_single_residual.cu:173-178](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L173-L178)：未攒满时的 else 分支——新 token 直接追加在残余缓冲第 `new_lens` 个位置，主缓存不动；
- [test_single_residual.cu:180-182](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L180-L182)：`seqlen_kv` 累加后重算 `residual_len` 与 `new_lens`，交给第二次调用；
- [test_single_residual.cu:191-214](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L191-L214)：第二次 `mha_fwd_kvcache` 与第二次比对，逻辑同 Round 0。

**main 的扫描点设计**：

- [test_single_residual.cu:239-302](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L239-L302)：以 1024 为基准，取 `base-33、base-32、base-10、base-1、base+31、base+32、base+128±x、base+256±x` 共 17 个点。这些偏移不是随手写的：它们让 `seqlen_kv % 128` 遍历「余 1、余 32 附近、整除、接近整除」等各种边界，覆盖 residual kernel 的掩码与攒满触发逻辑；第 246-248 行写死 `k-channel / 4bit / group_size=128`。

#### 4.3.4 代码实践

**实践目标**：用 kernel 级测试得到 k-channel / 4bit / **group_size=32** 配置的误差数据（这是 CMake 路径打通后的正餐）。

1. 确认 `libs/cutlass` 子模块已初始化（`git submodule update --init`）；
2. 修正 [CMakeLists.txt:10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L10) 的 include 路径（方法见 4.4.4）；
3. 把 [test_single_residual.cu:248](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L248) 的 `group_size = 128` 改成 `32`（dispatch 已支持该组合，见 4.4.3 的证据，无需改 genfile）；
4. 编译并运行 `test_single_residual`，记录 17 个扫描点 × 2 轮的 `max_error / MAE / MSE`。

**需要观察的现象**：`test pass !` 应出现在全部 34 组输出中；group_size=32 的参数更多、每组 scale 更精细，误差量级应不劣于（通常小于）group_size=128 的结果。

**预期结果**：`max_error` 通常比 MAE 大一个数量级左右（离群点来自个别通道的量化舍入）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`main` 传入 `seqlen_kv = 1024 + 256`（即 1280）时，Round 0 的残余区长度是多少？Round 1 走 if 还是 else 分支？

**答案**：1280 % 128 == 0，按第 24 行的特殊规则 `residual_len = 128`，打包区为 1152。于是 Round 1 进入 if 分支（`new_lens == residual_block_size`）：`*_new` 被拼回主缓存，残余清零后只放 1 个新 token。这正是「攒满即回写」的触发用例。

**练习 2**：为什么 Round 1 结束时 `seqlen_kv = seqlen_kv + new_lens + seqlen_new`（第 180 行）把 `new_lens` 也加进去，而不是只加 1？

**答案**：Round 1 进入 if 分支时，主缓存通过 `torch.cat` 增长了 `new_lens`（一整块 128），残余区又接收了 1 个新 token，两段都要计入总序列长度，才能让参考实现的 `torch.cat({K_host_cat, K_new_host})` 与被测路径看到同一条序列。else 分支时 `new_lens` 个残余 token 依然留在残余区没进主缓存，但总长同样 = 打包区 + 残余 + 新增 1，公式对两个分支都成立。

**练习 3**：这个测试如果失败了，你能定位到哪个 kernel 吗？

**答案**：不能精确定位到单个，但能缩小范围：Round 0 的失败涉及 qpack、splitkv、residual、combine 全部四个 kernel（以及反量化）；Round 1 独有的失败（Round 0 过、Round 1 挂）则强烈指向 residual kernel 的原位再量化与 `torch.cat` 回写路径。要进一步隔离，需借助 `test_single_packdecode`（只测打包+解码，无残余）——这正是它被设计出来的原因。

### 4.4 CMakeLists.txt：target 定义、被注释的测试与编译链

#### 4.4.1 概念说明

pip 安装的 `bit_decode_cuda` 只暴露 pybind 函数，不给你直接调 C++ 模板的入口；.cu 测试需要一条**独立的构建通道**：CMake 直接编译「测试 main + 5 个 genfile 实例化单元」，链接 `find_package(Torch)` 得到的 ATen 库。`CMakeLists.txt` 就是这条通道的定义处——同时它也是本讲最重要的「考古现场」：三个测试 target 里两个被注释、一个 bench 全被注释、include 路径硬编码到作者个人目录。读懂它为什么是这个状态，等于读懂了仓库的工程演进史。

#### 4.4.2 核心流程

```text
cmake -B build -S csrc/bit_decode
  → find_package(Torch)
  → add_executable(test_single_residual, 测试.cu + 5×genfile.cu)
  → target_link_libraries(TORCH_LIBRARIES)
  → target_include_directories(${INCLUDE_DIR})   ← 指向 cutlass（当前硬编码，有坑）
  → nvcc -maxrregcount=255 -gencode sm_80 -w
cmake --build build --target test_single_residual
./build/test_single_residual
```

#### 4.4.3 源码精读

**include 路径的双重问题**：

- [CMakeLists.txt:9-10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L9-L10)：被注释的第 9 行 `${PROJECT_SOURCE_DIR}/../../libs/cutlass/include` 是**正确形态**；生效的第 10 行硬编码到作者个人目录 `/home/ddy/Projects/BitDecoding/libs/cutlass`——在任何其他机器上必然找不到。而且注意它指向 cutlass **仓库根**而非其 `include/` 子目录：源码里 include 的是 `<cutlass/numeric_types.h>`、`<cute/tensor.hpp>`（如 [kernel_traits.h:11](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L11)），这些头文件位于 `libs/cutlass/include/` 之下，所以路径必须落在 `include` 子目录。权威参照是 pip 扩展自己的构建配置：[setup.py:159-163](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L159-L163) 中 `include_dirs` 明确写了 `libs/cutlass/include`。

**唯一启用的 target**：

- [CMakeLists.txt:35-46](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L35-L46)：`test_single_residual` 的完整定义——源列表 = 测试 .cu + 5 个 genfile（与 [setup.py:126-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L126-L136) 的 CUDAExtension 源列表中 genfile 部分完全一致，这就是 2.4 节说的「实例化集合」镜像）；链接 Torch 库；编译选项 `-maxrregcount=255 -gencode arch=compute_80,code=sm_80 -w`（限寄存器、只编 sm_80、关警告）。

**被注释的两个测试 target**：

- [CMakeLists.txt:22-33](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L22-L33)：`test_single_packdecode`（本讲实践的主角）；
- [CMakeLists.txt:48-59](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L48-L59)：`test_batch_packdecode`（paged-KV 批量测试）；
- [CMakeLists.txt:61-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L61-L85)：两个 bench target 同样被注释（下一讲 u7-l2 的素材）。

**为什么被注释：调用签名过期（本讲最关键的证据链）**。当前 `mha_fwd_kvcache` 的签名是 18+ 个参数、返回 5 元组：

- [flash_api.h:313-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L313-L341)：现签名要求 `k_ / v_ / seqlens_k_` 三个 optional、`k_pack_new / k_params_new / v_pack_new / v_params_new` 四个输出缓冲，返回 `std::tuple` 五元组；
- [test_single_packdecode.cu:64-70](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_packdecode.cu#L64-L70)：该测试的调用只传了 `Q、k_pack、k_params、v_pack、v_params、opt_block_table、sm_scale、quant_mode、group_size` 九个实参，且用单个 `at::Tensor out` 接返回值——这是**旧版 API**（残余机制引入之前的形态），与现签名既不匹配参数个数也不匹配返回类型；
- [test_batch_packdecode.cu:130-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_batch_packdecode.cu#L130-L136)：同样的旧式调用。也就是说，即便修好 include 路径、取消注释，这两个 target 也**必然编译失败**（`no matching function for call to 'mha_fwd_kvcache'` 或返回类型无法转换）。它们是 API 演进留下的化石：作者把残余机制合入后只更新了 residual 测试，另外两个测试未跟着改，索性注释掉 target。

**kernel 层面实践配置是可用的**。实践任务要求的 k-channel / 4bit / group_size=32 在 dispatch 与实例化两层都已启用：

- [flash_api.h:199-214](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L199-L214)：dispatch 中 k-channel 分支的 group_size 128（第 199 行）与 32（第 203 行）是启用状态，64 与全部 k-tensor 分支被注释；
- [genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu:12-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu#L7-L14)：4-bit splitkv 的显式实例化中 `<..., 1, 4, 128>`（第 12 行）与 `<..., 1, 4, 32>`（第 14 行）未注释（第 7-9 行的 k-tensor `0` 模式被注释）。qpack 侧同理。

结论：**障碍全在构建层与测试代码的陈旧调用，不在 kernel 能力**。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：按任务恢复 `test_single_packdecode` target 并编译运行，记录 k-channel / 4bit / group_size=32 配置的最大误差；编译不通则产出错误分析报告。

**操作步骤**（以下 1-4 步会修改 `CMakeLists.txt`——这是构建配置而非 kernel 源码，属于本实践授权范围；请在独立分支上做）：

1. 初始化子模块：`git submodule update --init`，确认 `libs/cutlass/include/cutlass/` 存在；
2. 修 include 路径：把 [CMakeLists.txt:10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L10) 改回注释第 9 行的形态 `set(INCLUDE_DIR ${PROJECT_SOURCE_DIR}/../../libs/cutlass/include)`（指向 `include` 子目录，理由见 4.4.3）；
3. 取消 [CMakeLists.txt:22-33](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L22-L33) 的注释，恢复 `test_single_packdecode` target；
4. 配置并编译：
   ```bash
   cd csrc/bit_decode
   cmake -B build . && cmake --build build --target test_single_packdecode -j
   ```
5. 若（如 4.4.3 所预测）编译失败，**不要急于改测试代码**，先写错误分析报告，模板如下：

```text
# test_single_packdecode 编译错误分析报告
1. 环境记录：CUDA 版本 / torch 版本 / GPU 架构 / cutlass 子模块 commit
2. 修改记录：CMakeLists 第 10 行 → 新路径；第 22-33 行取消注释
3. 编译命令与完整报错（贴第一条模板报错即可）：
   预期报错点：test_single_packdecode.cu:64，mha_fwd_kvcache 无匹配重载
4. 根因分析：
   调用点（test_single_packdecode.cu:64-70）为 9 实参 + 单返回值；
   当前签名（flash_api.h:313-341）要求 k_/v_/seqlens_k_ + 4 个 *_new
   缓冲共 18 个形参，返回 5 元组 → 参数个数与返回类型双重不匹配。
5. 佐证：test_single_residual.cu:101-112 是新签名的正确用法。
6. 修复选项与代价评估（见下方三条路线）。
```

6. **修复路线（按代价从低到高）**：
   - **路线 A（零修改替代验证）**：放弃 `test_single_packdecode`，直接编译运行唯一活跃的 `test_single_residual`（调用已是新签名），把其 main 中 group_size 改为 32 即满足「k-channel/4bit/g=32 + max_error」的数据需求（见 4.3.4）；
   - **路线 B（迁移调用）**：参照 [test_single_residual.cu:101-112](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L101-L112) 的写法重写 `test_single_packdecode.cu:64-70` 的调用：补 `std::nullopt` 风格的三个 optional（无残余时 `k_/v_` 可传残余缓冲或按 residual 测试的方式给全零缓冲 + `new_lens`）、四个 `*_new` 输出张量，并用结构化绑定接 5 元组；同时把 [test_single_packdecode.cu:79-88](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_packdecode.cu#L79-L88) 的误差统计加一行 `float max_error = diff.abs().max().item<float>();`（该测试原本只算 MAE/MSE，而实践任务要求记录最大误差），main 的 group_size（[第 114 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_packdecode.cu#L114)）也要从 128 改成 32；
   - **路线 C（不推荐）**：在 flash_api.h 加旧签名的兼容重载——为测试反改生产代码，得不偿失。

**需要观察的现象**：步骤 4 的编译输出。两条典型失败路径：(a) 第 2 步没做对时报 `cutlass/numeric_types.h: No such file or directory`（include 路径问题）；(b) include 修好后报 `no matching function for call to 'mha_fwd_kvcache'`（签名过期问题，预期主失败）。

**预期结果**（诚实声明：本讲义写作环境无 GPU、子模块未初始化，以下均为基于源码静态分析的预测，**待本地验证**）：
- 编译大概率失败于签名不匹配（证据见 4.4.3）；
- 若走路线 A 或 B 成功运行，k-channel / 4bit / group_size=32 单 batch、seqlen 1024 的配置下，MAE 应在 1e-2 量级、max_error 高一个数量级左右、判定为 `test pass !`（阈值 0.1）。

#### 4.4.5 小练习与答案

**练习 1**：如果不链 5 个 genfile、只编译测试 .cu 单个文件，会发生什么？

**答案**：编译（翻译）阶段能过——`flash_api.h` 只提供声明；链接阶段失败，报 `undefined reference to run_mha_fwd_splitkv_dispatch<...>()` 与 `run_kvcache_qpack_<...>()` 等符号。因为这些模板函数的可执行体只存在于显式实例化它们的 genfile 翻译单元里（对照 [flash.h:203-205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L203-L205) 的声明）。

**练习 2**：`-maxrregcount=255 -w` 这两个编译选项各是什么用意？

**答案**：[CMakeLists.txt:46](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L46) 把寄存器上限设为 255（避免 kernel 因寄存器压力被编译器降低占用度；第五单元讲过解码 kernel 共享内存已超 48KB、靠 `cudaFuncSetAttribute` 抬限，寄存器同样要管理），`-w` 关闭全部警告——FlashAttention 系代码有大量已知告警，测试构建只要错误信号。

**练习 3**：`test_batch_packdecode` 与另两个测试的本质区别是什么？

**答案**：它是唯一测 **paged-KV** 路径的：[_generate_block_kvcache](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_batch_packdecode.cu#L16-L79) 生成 `(num_blocks, page_block_size=256, h, d)` 的分页缓存与 `randperm` 打乱的 block_table，参考侧用 `index_select(0, flat_block_table)` 按页表把分页缓存重排回稠密序列再算 `single_mha`（[第 142-152 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_batch_packdecode.cu#L142-L152)），阈值也最严（0.01）。可惜它同样停留在旧签名（第 130-136 行），是 paged 路径「参数已预留、测试路径未覆盖」现状（下一讲架构评审的素材）的直接证据。

## 5. 综合实践

**任务：搭建你自己的 kernel 级回归基线**。把本讲两层测试串成一条最小验证流水线：

1. **Python 层基线**（无 CMake 依赖，GPU 机器直接可跑）：运行 `evaluation/test.py`，把 32 轮 MAE 抄成一张表的两列（4bit/g=32 与 2bit/g=32，后者需手改第 42/45 行）；
2. **CMake 层打通**：按 4.4.4 步骤 1-2 修好 include 路径，先只编译 `test_single_residual`（唯一应成功的 target），确认 `cmake --build` 产物可执行；
3. **记录化石证据**：取消 `test_single_packdecode` 注释尝试编译，把第一条报错原文、报错行号、与 `flash_api.h:313-341` 的签名对照，写进一份不超过 20 行的错误分析报告（模板在 4.4.4 步骤 5）；
4. **对比两层结论**：回答——Python 层 MAE（第 1 步）与 kernel 层 MAE/max_error（第 2 步，group_size 改 32 后）数量级是否一致？若 kernel 层通过而 Python 层误差异常，嫌疑应指向哪段胶水代码？

**预期产出**：一张两层数据对照表 + 一份错误分析报告。第 4 问的参考答案：指向 Python 侧的缓存管理与参数传递（`DynamicCache.update_pack/update_residual` 的拼接维度、`new_lens/seqlens_k` 语义、补零对齐逻辑），因为 kernel 本体已被 kernel 级测试单独证实。全部运行数值**待本地验证**。

## 6. 本讲小结

- BitDecoding 的正确性验证分两层：`evaluation/test.py` 在 Python 层逐轮打印与 `attention_ref` 的 MAE（不判定），三个 .cu 测试在 kernel 层直接调 C++ 模板并对 MAE/MSE 设硬阈值（single/residual 为 0.1，batch 为 0.01）。
- `single_mha` 是三个 .cu 测试自包含的 einsum+softmax 参考实现，与 `attention_ref` 数学等价；它跟随输入设备决定在 CPU 或 GPU 上算参考。
- `TestDecodingKernelCorrectness`（residual 版）是唯一与当前 API 同步且启用的 kernel 级测试：固定种子、17 个边界扫描点、两轮闭环覆盖「打包→残余→攒满→torch.cat 回写→再 decode」；其残余切分规则（整除时保留整整一块）与 test.py（余 0 则残余为空）刻意不同。
- CMakeLists 是独立于 pip 的第二条构建通道：测试 target = 测试 .cu + 5 个 genfile 实例化单元；include 路径当前硬编码到作者个人目录且指向 cutlass 根（应为 `libs/cutlass/include`，参照 setup.py）。
- `test_single_packdecode` 与 `test_batch_packdecode` 是 API 演进的化石：调用旧版 9 参数/单返回值签名，与现 18+ 参数/5 元组签名不匹配，即便恢复 target 也必然编译失败——修复路线 A（用 residual 测试替代）代价最低。
- k-channel / 4bit / group_size=32 在 dispatch 与 genfile 实例化两层均已启用，测试打通的障碍全在构建层，不在 kernel 能力。

## 7. 下一步学习建议

- **下一讲 u7-l2（性能基准）**：CMakeLists 里被注释的两个 bench target（`bench_single_packdecode/bench_single_residual`，[第 61-85 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L61-L85)）与本讲的测试 target 共享同一构建通道与同样的化石问题；`evaluation/bench_throughput.py` 则把测量搬到模型级。建议先回头跑通本讲的 CMake 通道，再进下一讲。
- **延伸阅读**：`csrc/bit_decode/src/bench_single_packdecode.cu`（对照本讲的测试版，看计时循环怎么写）；`evaluation/ablation/` 下的 BitBLAS/Marlin 对比脚本（下一讲的消融基线）。
- **给动手者的挑战**：按 4.4.4 路线 B 把 `test_single_packdecode` 迁移到新签名并提交 PR——这是理解残余机制引入前后 API 差异的最佳练习，也是对上游最实际的贡献之一。
