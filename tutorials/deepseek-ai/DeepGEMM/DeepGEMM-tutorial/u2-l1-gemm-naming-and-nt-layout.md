# GEMM 命名约定与 NT 布局

## 1. 本讲目标

学完本讲你应该能够：

- 说出 DeepGEMM 的 GEMM 命名约定 \(D = C + A @ B\)，以及函数名后缀 `nt` / `nn` / `tn` / `tt` 各自的含义；
- 理解为什么 **NT 是「主布局 / 原生 kernel」**，而 `nn` / `tn` / `tt` 只是它的零拷贝转置包装；
- 解释 SM90 为什么只能用 NT、而 SM100 四种布局全支持，并能在源码里指出那个决定性的架构开关。

本讲承接 u1-l4：你已经会用 `(tensor, sf)` 元组做一次最小 FP8 GEMM 调用，这里我们退一步，把「函数名为什么这么长」「为什么 SM90 只有 nt」讲清楚。

## 2. 前置知识

- **矩阵乘法形状**：\(A @ B\) 中 \(A\) 是 \(M \times K\)，\(B\) 是 \(K \times N\)，结果是 \(M \times N\)。中间的 \(K\) 是「规约维 / reduction dim」。
- **行主序 / 列主序（row-major / column-major）**：决定元素在内存里的排列顺序。PyTorch 默认行主序。
- **major（主维）**：在 CUTLASS / CuTe 里，若某维的 stride 为 1（在内存里连续），就说张量是「该维 major」。例如形状 \([M, K]\) 的行主序张量，\(K\) 维 stride=1，称为 **K-major**。
- **张量核（tensor core）**：GPU 上做矩阵乘加的高速单元。SM90（Hopper）用 WGMMA 指令，SM100（Blackwell）用 UMMA 指令，它们对输入内存布局的要求不同——这正是本讲的根因。
- 约定提醒（来自 u1-l4）：DeepGEMM 的 FP8 输入是 `(tensor, sf)` 元组、输出 `D` 需要预分配，且 SF 的格式随架构变化。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `README.md` | 第 65 行的「Notices」段落，官方对命名约定与布局能力的总述。 |
| `csrc/apis/gemm.hpp` | 所有 GEMM 的宿主入口。本讲精读 `fp8_fp4_gemm_nt` / `nn` / `tn` / `tt`（行 73–164）。 |
| `csrc/utils/layout.hpp` | 判断输入 major、要求 D 行主序、判断「是否强制 K-major」的三个小工具（行 21–34）。 |
| `deep_gemm/__init__.py` | Python 侧导出函数名，可看到 `fp8_gemm_nt` 与 `fp8_fp4_gemm_nt` 是同一函数的别名（行 36–45）。 |

## 4. 核心概念与源码讲解

### 4.1 GEMM 命名约定

#### 4.1.1 概念说明

DeepGEMM 所有 GEMM 都遵循同一个数学约定：

\[
D = C + A @ B
\]

其中 \(A \in \mathbb{R}^{M \times K}\)、\(B \in \mathbb{R}^{K \times N}\)、\(C, D \in \mathbb{R}^{M \times N}\)。\(C\) 是可选的累加项（不传则视为 0）。

函数名末尾的两个字母 `{X}{Y}` 描述 **A 和 B 的存储布局**：`N` 表示该矩阵按「自然形状」存（non-transposed），`T` 表示存成了转置形式。四种组合对应的实际计算如下：

| 后缀 | 用户传入 A | 用户传入 B | 实际计算 |
|------|-----------|-----------|----------|
| `nt` | \([M,K]\) | \([N,K]\) | \(C + A B^{\mathsf T}\) |
| `nn` | \([M,K]\) | \([K,N]\) | \(C + A B\) |
| `tn` | \([K,M]\) | \([K,N]\) | \(C + A^{\mathsf T} B\) |
| `tt` | \([K,M]\) | \([N,K]\) | \(C + A^{\mathsf T} B^{\mathsf T}\) |

注意 `nt` 里 B 存成 \([N,K]\)，相当于把参与乘法的 \([K,N]\) 矩阵「转着存」，所以第二个字母是 `T`。这一点 README 有权威说明。

#### 4.1.2 核心流程

一次 GEMM 调用在宿主侧大致经过：

1. 用户按某个布局（`nt` / `nn` / `tn` / `tt`）传入 A、B、（可选 C）、预分配的 D。
2. 宿主 API 校验形状、dtype、以及 D 必须行主序。
3. （FP8 路径）把缩放因子 SF 变换成 kernel 所需的 TMA 布局。
4. 按 `device_runtime->get_arch_major()`（返回 9 或 10）派发到 SM90 或 SM100 的具体 kernel。

#### 4.1.3 源码精读

README 第 65 行一句话点明命名约定与两代架构的布局能力差异（SM90 仅 NT、SM100 全支持）：

[README.md:65](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L65) — 官方对 `D = C + A @ B`、NT 输入约定、以及架构布局能力差异的总述。

`fp8_fp4_gemm_nt` 是真正干活的「原生」函数，签名上方注释写明了形状约定 `[M, K] @ [N, K].T`：

[csrc/apis/gemm.hpp:73-82](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L73-L82) — `nt` 原生入口，注释 `// Shape must be [M, K] @ [N, K].T`。

它在末尾按架构派发：`arch_major==9` 走 SM90（`sm90_fp8_gemm_1d1d` 或 `1d2d`），`==10` 走 SM100：

[csrc/apis/gemm.hpp:110-123](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L110-L123) — `if (arch_major == 9 ...) sm90_fp8_gemm_1d1d ... else if (arch_major == 10 ...) sm100_fp8_fp4_gemm_1d1d`。

Python 侧，`fp8_gemm_nt` 和 `fp8_fp4_gemm_nt` 指向**同一个** C++ 函数（`register_apis` 里用 `m.attr(...) = m.attr(...)` 做了别名）：

[deep_gemm/__init__.py:36-45](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L36-L45) — 同时导出 `fp8_fp4_gemm_*` 与作为别名的 `fp8_gemm_*`。

#### 4.1.4 代码实践

（源码阅读 + 一行验证型）

1. **实践目标**：确认 `fp8_gemm_nt` 与 `fp8_fp4_gemm_nt` 是同一个函数对象。
2. **操作步骤**：在已安装 DeepGEMM 的环境里执行
   ```python
   import deep_gemm
   print(deep_gemm.fp8_gemm_nt is deep_gemm.fp8_fp4_gemm_nt)
   ```
3. **观察与预期**：输出 `True`。
4. **预期结果**：两个名字共享同一个底层实现，只是历史遗留别名。
5. 待本地验证（无 GPU / 未安装时，直接阅读 `csrc/apis/gemm.hpp:711-714` 的别名注册即可得到同样结论）。

#### 4.1.5 小练习与答案

**Q1**：函数名 `gemm_tn` 里两个字母分别描述谁？
**A**：第一个字母 `t` 描述 A（存成转置 \([K,M]\)），第二个字母 `n` 描述 B（自然形状 \([K,N]\)）。

**Q2**：`D = C + A @ B` 中，如果 K=0 会怎样？
**A**：参考 `early_return`（[csrc/apis/gemm.hpp:36-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L36-L40)），K=0 表示没有乘加要算，直接把 C 拷给 D（或置零）后提前返回。

---

### 4.2 NT 主布局与转置派生

#### 4.2.1 概念说明

本讲最重要的一个事实：**只有 `nt` 是一个真正的 kernel；`nn`、`tn`、`tt` 都只是「几行转置后转发给 `nt`」的薄包装**。

PyTorch 的 `.transpose(0, 1)` 只是交换 stride 的**零拷贝视图**，不搬运数据。所以这三个包装几乎不增加开销——这也是 DeepGEMM 敢于「只写一个 nt 内核」的前提。

#### 4.2.2 核心流程

对每个非 `nt` 布局，wrapper 用 `.transpose(0, 1)`（3D 分组版用 `.transpose(1, 2)`）把用户传入的 A/B 调整成 `nt` 期望的 \([M,K]\) / \([N,K]\) 形状，再原样调用 `fp8_fp4_gemm_nt`：

| wrapper | 对 A | 对 B | 等价计算 |
|---------|------|------|----------|
| `nn` | 不动 | `.transpose(0,1)` | \(A B\) |
| `tn` | `.transpose(0,1)` | `.transpose(0,1)` | \(A^{\mathsf T} B\) |
| `tt` | `.transpose(0,1)` | 不动 | \(A^{\mathsf T} B^{\mathsf T}\) |

注意：SF 张量（`a.second` / `b.second`）必须**跟着数据一起转置**，否则缩放因子会和错位的块对应。

#### 4.2.3 源码精读

`fp8_fp4_gemm_nn` 只转置 B（连同它的 SF），其余参数原样透传：

[csrc/apis/gemm.hpp:126-137](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L126-L137) — `fp8_fp4_gemm_nt(a, {b.first.transpose(0, 1), b.second.transpose(0, 1)}, ...)`。

`fp8_fp4_gemm_tn` 同时转置 A 和 B：

[csrc/apis/gemm.hpp:139-151](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L139-L151) — 两个操作数（含各自 SF）都 `.transpose(0, 1)`。

`fp8_fp4_gemm_tt` 只转置 A：

[csrc/apis/gemm.hpp:153-164](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L153-L164) — `{a.first.transpose(0, 1), a.second.transpose(0, 1)}, b`。

同样的「转置再转发 `nt`」模式在 BF16 路径（`bf16_gemm_nn/tn/tt`）和 cuBLASLt 基准路径（`cublaslt_gemm_nn/tn/tt`）里一模一样，说明这是全库统一的设计：

[csrc/apis/gemm.hpp:440-462](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L440-L462) — BF16 的 nn/tn/tt 同样只做 transpose 后转发 `bf16_gemm_nt`。

> 你注意到了吗：注册时 `nt`/`nn` 的 `compiled_dims` 默认是 `"nk"`，而 `tn`/`tt` 默认是 `"mn"`（[csrc/apis/gemm.hpp:649-672](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649-L672)）。这反映了不同布局下「哪个维度适合编译期特化」的差异，细节留到 u5-l3 讲。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `nn` / `tn` / `tt` 都是 `nt` 的转置包装，并填出转置映射表。
2. **操作步骤**：
   - 打开 `csrc/apis/gemm.hpp` 行 126–164，逐个记录 `nn` / `tn` / `tt` 对 `a.first`、`a.second`、`b.first`、`b.second` 调用的 `.transpose(dim0, dim1)`，填出 4.2.2 的表格。
   - （可选，需 SM90/SM100 GPU）构造一对 BF16 张量 `A=[M,K]`、`B=[K,N]`，分别调用 `deep_gemm.bf16_gemm_nn(A, B, D1)`，与「先把 B 转置成 \([N,K]\) 再调 `bf16_gemm_nt(A, B.t(), D2)`」比较 `D1`、`D2`。
3. **观察与预期**：`nn` 只转 B；`tn` 转 A 和 B；`tt` 只转 A。数值上 `D1` 与 `D2` 应一致（`calc_diff` 接近 0）。
4. **预期结果**：三种布局最终都进入同一个 `nt` 内核，区别只在调用前的零拷贝转置。
5. 待本地验证（无 GPU 时，源码阅读部分即可完成表格）。

#### 4.2.5 小练习与答案

**Q1**：为什么 wrapper 要把 SF 也一起 transpose？
**A**：SF 是和所属矩阵逐块对齐的缩放因子；数据转置后块在内存里的位置变了，SF 必须跟着转置才能继续保持「这块数据 × 这块 SF」的对应关系。

**Q2**：`.transpose(0, 1)` 会拷贝数据吗？为什么这点对性能重要？
**A**：不会，它只交换 stride，是零拷贝视图。正因零拷贝，`nn`/`tn`/`tt` 包装几乎没有开销，`nt` 才能安心作为唯一内核——否则每种布局都要单独写一份高性能 kernel。

---

### 4.3 架构布局能力差异

#### 4.3.1 概念说明

README 说「SM90 仅支持 NT，SM100 支持 NT/TN/NN/TT」。根因在于两代张量核对**操作数内存 major** 的要求不同：

- SM90 的 WGMMA 要求操作数是 **K-major**（最后一维连续）。
- SM100 的 UMMA 可以同时吃 K-major 或 MN-major。

`nt` 布局天然给出 K-major（A=\([M,K]\) 行主序、B=\([N,K]\) 行主序，K 维都连续）。而 `nn`/`tn`/`tt` 的转置会把某个操作数变成 **MN-major**，在 SM90 上就会被拒——这就是「SM90 只能用 nt」的本质。

#### 4.3.2 核心流程

判断逻辑被收在一个小函数里：

```
fp8_requires_k_major() = (get_arch_major() == 9)
```

- `arch_major == 9`（SM90）：返回 true → `nt` 入口断言 A、B 必须 K-major → 只有 NT 天然满足。
- `arch_major == 10`（SM100）：返回 false → 不断言 → 四种布局都能进。

#### 4.3.3 源码精读

三个工具函数都在 `csrc/utils/layout.hpp`。`get_major_type_ab` 用最后一维 stride 判定 major：

[csrc/utils/layout.hpp:21-24](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L21-L24) — `stride(-1) == 1` 判为 K-major，否则 MN-major。

`check_major_type_cd` 要求输出 D（和 C）行主序，注释明说「只支持行主序输出」：

[csrc/utils/layout.hpp:26-30](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L26-L30) — `DG_HOST_ASSERT(t.stride(-1) == 1)`。

而 `fp8_requires_k_major` 就是全库 SM90/SM100 布局能力差异的开关：

[csrc/utils/layout.hpp:32-34](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L32-L34) — `return device_runtime->get_arch_major() == 9;`。

回到 `gemm.hpp`，`nt` 入口正是用它做架构相关的 K-major 断言：

[csrc/apis/gemm.hpp:85-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L85-L88) — `if (fp8_requires_k_major()) { 断言 major_a==K 且 major_b==K; }`。

因此在 SM90 上调 `nn`/`tn`/`tt`：wrapper 先把某操作数 transpose 成 MN-major → 进入 `nt` → 命中第 87 行的 K-major 断言失败 → 报错。这就是「SM90 上仍只走 nt」的代码层原因。

#### 4.3.4 代码实践

1. **实践目标**：用源码追踪确认 SM90 的 K-major 约束如何把布局锁死在 NT。
2. **操作步骤**（纯源码追踪）：
   - 假设在 SM90 上调用 `fp8_gemm_nn`。
   - 跟踪：`nn`（[csrc/apis/gemm.hpp:135](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L135)）→ 把 B transpose 成 MN-major → 调 `fp8_fp4_gemm_nt` → `fp8_requires_k_major()` 为 true → 第 87 行 `DG_HOST_ASSERT(major_b == K)` 失败。
   - 写出这条失败链路，并标注失败发生在第 87 行。
3. **观察与预期**：能画出「`nn` → transpose → `nt` → 断言失败」的调用链。
4. **预期结果**：SM90 上 `nn`/`tn`/`tt` 都会在 K-major 断言处终止；只有 `nt` 能正常派发到 `sm90_fp8_gemm_1d1d`。
5. 待本地验证（在真实 SM90 卡上运行会得到 `DG_HOST_ASSERT` 错误信息）。

#### 4.3.5 小练习与答案

**Q1**：如果把 `fp8_requires_k_major()` 改成总返回 false，SM90 上会发生什么？
**A**：宿主断言不再触发，但 SM90 的 WGMMA kernel 实际无法正确处理 MN-major 操作数，会在 GPU 端得到错误结果或崩溃——说明这个开关反映的是真实硬件约束，而非随意限制。

**Q2**：SM100 上调用 `nn`，最终落到哪个 kernel？
**A**：仍是 `sm100_fp8_fp4_gemm_1d1d`（[csrc/apis/gemm.hpp:119](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L119)）。因为 `nn` 只是 transpose 后转发 `nt`，而 `nt` 在 `arch_major==10` 时派发到它。

## 5. 综合实践

给定 \(M=128,\ K=256,\ N=512\)，完成下面三问，把本讲三块知识串起来：

1. 写出 `nt` / `nn` / `tn` / `tt` 四种调用下，用户应分别以什么形状传入 A 和 B（对照 4.1.1 表格）。
2. 对每种布局，写出它最终交给 `fp8_fp4_gemm_nt` 时 A、B 的形状（都应是 \([M,K]\) 与 \([N,K]\)）。
3. 解释为什么四种布局最终进入 `nt` 后，`nt` 看到的形状完全一致。

**参考答案**：

1. `nt`: A \([128,256]\), B \([512,256]\)；`nn`: A \([128,256]\), B \([256,512]\)；`tn`: A \([256,128]\), B \([256,512]\)；`tt`: A \([256,128]\), B \([512,256]\)。
2. 四种布局经 wrapper 转置后，进入 `nt` 的都是 A=\([128,256]\)、B=\([512,256]\)。
3. 因为 `nn`/`tn`/`tt` 的 transpose 正是为了把任意布局**归一化**成 `nt` 期望的 \([M,K]\) / \([N,K]\) 形状。`nt` 内部形状恒定，这是它能作为唯一原生 kernel 的前提；而 SM90 只支持 NT，则是因为这套归一化产生的 MN-major 张量不被 SM90 的 WGMMA 接受（4.3）。

## 6. 本讲小结

- 所有 GEMM 遵循 \(D = C + A @ B\)，后缀 `nt` / `nn` / `tn` / `tt` 描述 A、B 的存储布局（`N`=自然、`T`=转置）。
- **`nt` 是唯一的原生 kernel**；`nn` / `tn` / `tt` 只是零拷贝 `.transpose()` 后转发给 `nt` 的薄包装（FP8 / BF16 / cuBLASLt 三条路径都如此）。
- 转置时，数据张量和它的 SF 必须一起转置，保持逐块对应。
- SM90 的 WGMMA 强制 K-major，只有 NT 天然满足；SM100 的 UMMA 放宽限制，四种布局全支持。
- 这个架构差异由一个开关统一控制：`fp8_requires_k_major() == (get_arch_major() == 9)`。
- D（和 C）始终要求行主序（`check_major_type_cd`）。

## 7. 下一步学习建议

- 下一讲 **u2-l2「缩放因子 recipe 与 UE8M0 打包」** 会深入 SF 的形状与变换——本讲多次提到的「SF 跟着转置」在那里展开为完整的 recipe / 打包机制。
- 想看 `nt` 内部如何按架构派发到 `1d1d` / `1d2d`，可先跳读 **u2-l3「C++ 绑定与 API 派发层」**。
- `compiled_dims` 的 `"nk"` vs `"mn"` 默认差异，留到 **u5-l3「compiled_dims 与运行时调优旋钮」** 系统讲解。
