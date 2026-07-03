# 对称/Hermitian 特征值 eigh / eigvalsh

## 1. 本讲目标

本讲专讲 `scipy.linalg` 中面向**对称矩阵**（实数）与 **Hermitian 矩阵**（复数）的特征值求解入口 `eigh` 与 `eigvalsh`。学完本讲，你应当能够：

- 说清 `eigh` 相比上一讲的一般特征值函数 `eig`（见 u4-l1）为什么在对称/Hermitian 情况下更优：实特征值、正交特征向量、更快更稳、还能只算一部分。
- 掌握 `subset_by_index`（按下标取一段）与 `subset_by_value`（按数值区间取一段）两种「只求部分特征值」的用法，并理解它们底层如何翻译成 LAPACK 的 `range='I'` / `range='V'`。
- 理解 `driver`（`evr`/`evd`/`evx` 等标准驱动，`gv`/`gvd`/`gvx` 等广义驱动）的选择逻辑与适用差异。
- 认识辅助函数 `_check_select` 的职责，并理解它服务的是**带状/三对角**家族（`eig_banded`、`eigh_tridiagonal`）的旧式 `select` API，而非 `eigh` 的新式 subset API。

## 2. 前置知识

### 2.1 对称矩阵与 Hermitian 矩阵

- **实对称矩阵**：\(A\) 的元素都是实数，且 \(A = A^{\mathsf{T}}\)，即 \(a_{ij} = a_{ji}\)。
- **Hermitian 矩阵**：\(A\) 可以含复数，且 \(A = A^{\mathsf{H}}\)，其中 \(A^{\mathsf{H}}\) 表示「先转置再取共轭」。对于实矩阵，\(A^{\mathsf{H}} = A^{\mathsf{T}}\)，所以实对称是 Hermitian 的特例。

这两类矩阵有一条极其重要的谱定理（spectral theorem）：特征值**全是实数**，且可以选出一组**两两正交归一**的特征向量。写成矩阵形式：

\[
A = V \Lambda V^{\mathsf{H}}, \qquad V^{\mathsf{H}} V = I, \qquad \Lambda = \mathrm{diag}(\lambda_1, \dots, \lambda_n)
\]

其中 \(V\) 是酉矩阵（实数情形即正交矩阵），\(\Lambda\) 是实对角矩阵。这与一般方阵 \(A = X \Lambda X^{-1}\) 的对角化形成鲜明对比：一般矩阵的特征值可能为复数、特征向量也未必正交。

### 2.2 为什么要为对称/Hermitian 单独造函数

上一讲的 `eig` 用的是面向一般方阵的 QR 算法（LAPACK `?geev`/`?ggev`）。它**不知道**矩阵有对称结构，因此：

- 即便输入是对称矩阵，它也会返回复数 dtype 的特征值/特征向量（虚部为 0）。
- 计算量更大（一般特征值问题的代价显著高于对称问题），数值稳定性也更弱。
- 无法利用「只需存一半」这一存储技巧。

而 `eigh` 调用专门的对称/Hermitian 驱动（LAPACK 中以 `sy`（实）或 `he`（复）为前缀的例程，如 `syevr`、`heevr`），能保证返回**实特征值**与**正交归一的特征向量**，计算量约为一般问题的若干分之一，且数值上更稳定，还支持「只求一部分特征值」这种 `eig` 做不到的操作。

### 2.3 只存一半：`lower` / `uplo`

由于对称/Hermitian 矩阵的上三角和下三角互为（共轭）转置，只需存储其中一半。LAPACK 习惯用一个字符约定：

- `'L'`（Lower）：只用下三角；
- `'U'`（Upper）：只用上三角。

`eigh` 用更直观的布尔参数 `lower`（默认 `True`）来表达，内部再翻译成 `'L'`/`'U'`。注意：函数**不会**校验矩阵是否真的对称，它只是机械地读取你指定的那一半，另一半被忽略。如果你传了一个非对称矩阵却用 `eigh`，**不会报错，但结果会是错的**。

### 2.4 承接的前置认知

本讲建立在 u4-l1（一般特征值 `eig`）之上。请回忆：`eig` 是「校验 → dtype/内存归一化 → 覆写门控 → 委派 → 后处理」的薄壳，真正的 QR 迭代在 C++ 批量后端 `_batched_linalg` 里完成，并由 `_check_format_errors_warnings` 翻译错误。本讲的 `eigh` 走的却是**另一条路**——直接调用 f2py 包装的 LAPACK 例程（`get_lapack_funcs`），批处理则由 Python 层的 `@_apply_over_batch` 装饰器完成，这一点会在源码精读中重点对比。

## 3. 本讲源码地图

所有逻辑都集中在同一个文件里：

| 文件 | 角色 |
| --- | --- |
| `scipy/linalg/_decomp.py` | 特征值问题主文件，含 `eig`、`eigvals`、`eigh`、`eigvalsh`、`eig_banded`、`eigvals_banded`、`eigh_tridiagonal`、`eigvalsh_tridiagonal`、`hessenberg`、`cdf2rdf` 以及本讲关心的 `_check_select`。 |

本讲引用的关键代码点（全部位于 `_decomp.py`）：

- `eigh` 函数：装饰器与签名、参数校验、subset 处理、driver 选择、LAPACK 调用、错误翻译。
- `eigvalsh` 函数：一行委派到 `eigh`。
- `_check_select` 与 `_conv_dict`：服务于带状/三对角家族的旧式 select 校验。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`eigh`**：对称/Hermitian 特征值（含特征向量）的主入口，也是本讲的主体。
2. **`eigvalsh`**：只求特征值的薄封装。
3. **`_check_select` 与 subset 选择参数体系**：校验选择参数，并理清它与 `eigh` 的 subset API 之间的区别。

### 4.1 `eigh`：对称/Hermitian 特征值主入口

#### 4.1.1 概念说明

`eigh` 同时支持两类问题：

- **标准问题**：\(A v = \lambda v\)（不传 `b`）。
- **广义问题**：\(A v = \lambda B v\)（传入正定的 `b`），其中要求 \(B\) 正定。

求解结果满足（见 docstring 的数学约定）：

\[
A v_i = \lambda_i B v_i,\qquad v_i^{\mathsf{H}} A v_i = \lambda_i,\qquad v_i^{\mathsf{H}} B v_i = 1
\]

标准问题就是 \(B = I\) 的特例。广义问题中 `type` 参数（取 1/2/3）选择不同的归一化约定。

`eigh` 的核心价值点有四：

1. **实特征值 + 正交特征向量**：由对称/Hermitian 结构天然保证。
2. **更快更稳**：专用驱动 `?syevr`/`?heevr` 等。
3. **只算一部分**：通过 `subset_by_index` / `subset_by_value` 只取一段特征值，对大矩阵尤为有用。
4. **批处理**：通过 `@_apply_over_batch` 装饰器支持「一叠矩阵」的输入。

#### 4.1.2 核心流程

`eigh` 的 Python 层是一条标准的「校验 → 归一化 → 编码 → 委派 → 截断 → 翻译错误」流水线：

```
eigh(a, b, lower, eigvals_only, subset_by_*, driver)
│
├─ 1. 设置 uplo（'L'/'U'）与 _job（'N' 求值 / 'V' 求值+向量）
├─ 2. 校验 driver 字符串是否合法
├─ 3. asarray 校验 + check_finite；校验方阵；处理空矩阵
├─ 4. 计算 overwrite_a；判断 cplx（a 是否复数）
├─ 5. 若有 b：校验方阵、形状一致、type∈{1,2,3}，cplx 取 a|b 的或
├─ 6. subset 校验：
│     - subset_by_index  → range='I', il=lo+1, iu=hi+1（Fortran 1 基）
│     - subset_by_value  → range='V', vl=lo, vu=hi
├─ 7. 选 prefix：复数 'he'，实数 'sy'
├─ 8. 选 driver：显式给则校验兼容性；否则默认 evr（标准）/gvd（广义全量）/gvx（广义子集）
├─ 9. lwork 两步法查询最优工作数组长度
├─ 10. 调用 LAPACK 例程 drv(a, b, ...)，得 (w, v, ..., m, info)
├─ 11. 若 subset：用 m 截断 w、v（只保留真正命中的特征值个数）
└─ 12. 按 info 翻译错误：info==0 成功；info<-1 非法参数；
            info>n 说明 B 非正定；其余按 driver 给出收敛失败信息，统一抛 LinAlgError
```

关键的「编码」细节是：`eigh` 把 Python 风格的、对人类友好的参数，翻译成 LAPACK 的 Fortran 风格参数。例如 `lower=True` → `uplo='L'`、`eigvals_only=True` → `jobz='N'`、`subset_by_index=[1,4]` → `range='I', il=2, iu=5`（注意 0 基转 1 基）。

#### 4.1.3 源码精读

**装饰器与签名** —— 注意 `@_apply_over_batch` 是 `eigh` 获得批处理能力的地方：

[_decomp.py:L311-L314](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L311-L314)：装饰器 `@_apply_over_batch(('a', 2), ('b', 2))` 表示把 `a`、`b` 的前导批处理维度剥离，对每个 2D 切片循环调用本函数。也就是说，`eigh` 的批处理是**在 Python 层逐片循环**完成的，每片仍走下面的 f2py LAPACK 路径。这与 u4-l1 的 `eig`（批处理在 C++ 后端原生完成）形成对照。

**第一步：设置 uplo 与 job**

[_decomp.py:L477-L480](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L477-L480)：把布尔 `lower` 翻译成字符 `uplo`，把 `eigvals_only` 翻译成 `_job`（`'N'`=只算值，`'V'`=值+向量）。

**driver 合法性校验**

[_decomp.py:L482-L485](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L482-L485)：用一张白名单 `drv_str` 校验传入的 `driver` 字符串，非法值直接抛 `ValueError`。

**输入校验、空矩阵、覆写判定**

[_decomp.py:L487-L505](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L487-L505)：`_asarray_validated` 做 `check_finite`；校验 `a` 是方阵；对空矩阵做特殊处理（先对 2×2 单位阵求解以确定返回 dtype，再返回空数组）；`overwrite_a = overwrite_a or _datacopied(a1, a)`——即若输入已被强制拷贝过，就顺势允许覆写（详见 u2-l2 的 `_datacopied`）；用 `iscomplexobj` 判断是否复数。

**广义问题的 b 校验**

[_decomp.py:L507-L520](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L507-L520)：传入 `b` 时校验其方阵性、与 `a` 形状一致、`type∈{1,2,3}`，并把 `cplx` 取为「a 复数 或 b 复数」。

**subset 参数校验（本讲重点之一）**

[_decomp.py:L522-L545](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L522-L545)：这段把 `subset_by_index` / `subset_by_value` 翻译成 LAPACK 参数。要点：

- 两者**不能同时**给（第 525-526 行）。
- `subset_by_index=[lo, hi]`：校验 `0 <= lo <= hi < n`，再翻译成 `range='I', il=lo+1, iu=hi+1`——**加 1 是因为 Fortran 是 1 基索引**。
- `subset_by_value=[lo, hi]`：定义半开区间 \((lo, hi]\)，校验 `lo < hi`，翻译成 `range='V', vl=lo, vu=hi`。

**选前缀与 driver**

[_decomp.py:L547-L564](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L547-L564)：前缀 `pfx = 'he' if cplx else 'sy'`，最终调用的例程名是 `pfx + driver`（如 `'syevr'`、`'heevr'`、`'sygvd'`）。driver 选择逻辑：

- 显式给出 `driver` 时，校验它和「是否有 b」「是否请求 subset」三者兼容。例如 `ev`/`evd`/`gv`/`gvd` **不支持** subset（第 559-560 行会报错）。
- 未给时取默认值：标准问题用 `'evr'`；广义全量用 `'gvd'`；广义子集用 `'gvx'`。

> 小贴士：docstring 里对各 driver 的取舍有清楚说明——最慢最稳的是经典 `ev`（对称 QR）；最通用最优的是 `evr`（MRRR 算法）；`evd` 在某些情形更快但更耗内存；`evx` 只在「大矩阵只要极少数特征值」时才偶尔占优。

**lwork 查询与 LAPACK 调用（标准问题）**

[_decomp.py:L566-L588](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L566-L588)：先用 `get_lapack_funcs` 同时取出驱动例程 `drv` 和对应的 `_lwork` 查询例程；用 `_compute_lwork` 以「两步法」（先传 `-1` 查询最优长度，再用真实长度重算，承接 u3-l3 的 `safecall` 思想）拿到工作数组长度。注意 `evd`/`evr` 这类驱动需要**多个**工作长度变量（`lwork`/`liwork`/`lrwork`），代码用 `lwork_spec` 字典把返回的元组拆开。最后 `drv(a=a1, ...)` 真正求解，返回 `(w, v, *other_args, info)`。

**广义问题调用**

[_decomp.py:L590-L604](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L590-L604)：广义驱动用 `uplo`（而非 `lower`）和 `jobz`（而非 `compute_v`）。`gvd` 没有 lwork 查询接口，直接调用；其余先查 lwork 再调用。

**subset 结果截断**

[_decomp.py:L606-L608](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L606-L608)：LAPACK 返回的 `other_args[0]` 是**真正命中**的特征值个数 `m`（subset 模式下可能少于请求的区间宽度，因为数值区间里可能本就没那么多特征值）。代码据此把 `w` 截到前 `m` 个、`v` 截到前 `m` 列。

**info 错误翻译**

[_decomp.py:L610-L647](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L610-L647)：`info==0` 成功返回；`info<-1` 表示第 `-info` 个参数非法；`info>n` 说明广义问题中 `B` 的第 `info-n` 阶顺序主子式不正定（即 `B` 非正定，无法分解）；其余情况按 driver 类型给出收敛失败的具体文案，统一抛 `LinAlgError`。注意这里和 `solve`（u2-l2）的三档处理不同——`eigh` 的失败一律是硬错误 `LinAlgError`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `eigh` 返回实特征值与正交特征向量，并对比 `eig` 的复数返回。

**操作步骤**（示例代码，可直接运行）：

```python
import numpy as np
from scipy.linalg import eigh, eig

# 构造一个实对称矩阵（A = A^T）
B = np.random.rand(5, 5)
A = B + B.T

# 用 eigh 求解
w, v = eigh(A)
print("eigh 特征值 dtype:", w.dtype)        # 预期 float64（实数）
print("正交性误差 ||V^T V - I||:", np.linalg.norm(v.T @ v - np.eye(5)))  # 预期 ~0
print("重构误差 ||A - VΛV^T||:", np.linalg.norm(A - v @ np.diag(w) @ v.T))  # 预期 ~0

# 对比 eig：即便矩阵对称，eig 仍返回复数 dtype
w2, v2 = eig(A)
print("eig 特征值 dtype:", w2.dtype)        # 预期 complex128
```

**需要观察的现象**：

- `eigh` 的特征值是**实数**（`float64`），且 `v` 是正交矩阵（`v.T @ v ≈ I`）。
- `eig` 即使输入对称矩阵，也返回 `complex128`（虚部为 0），且特征向量不一定正交。

**预期结果**：正交性误差与重构误差都应在 \(10^{-15}\) 量级。如果你的重构误差远大于此，多半是矩阵规模或 dtype 问题，可改用 `np.allclose` 判断。

> 待本地验证：实际数值会因随机种子而异，但「误差极小」这一结论应稳定成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eigh` 不校验输入矩阵是否真的对称？这样做有什么好处和风险？

> **参考答案**：为了让用户可以只存储/填写矩阵的一半（上三角或下三角），另一半无需正确填写——这在大型对称矩阵存储中能省一半内存。风险是：若矩阵其实不对称，`eigh` **不会报错**，只会读取指定的一半并给出错误结果。所以对称性应由调用方保证。

**练习 2**：`eigh` 的 `subset_by_index=[0, 2]` 在传给 LAPACK 时，`il`、`iu` 分别是多少？为什么？

> **参考答案**：`il=1`、`iu=3`。因为 Python 是 0 基索引、LAPACK 的 Fortran 例程是 1 基索引，源码里对 `lo`、`hi` 各加 1（见 [_decomp.py:L536](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L536)）。

### 4.2 `eigvalsh`：只求特征值的薄封装

#### 4.2.1 概念说明

很多场景只需要特征值、不需要特征向量，例如：

- **判断矩阵正定性**：特征值全正 ⇔ 正定。
- **主成分分析（PCA）**：协方差矩阵的大特征值代表主成分方差。
- **谱聚类、谱半径估计**：只关心特征值的分布。

对这些场景，`eigvalsh` 是一个语义清晰的入口。本质上，它就是 `eigh(..., eigvals_only=True)` 的一行简写。

#### 4.2.2 核心流程

`eigvalsh` 的函数体没有任何独立逻辑，它把全部参数（包括 `subset_by_index`/`subset_by_value`/`driver`）原样转发给 `eigh`，并固定 `eigvals_only=True`：

```python
return eigh(a, b=b, lower=lower, eigvals_only=True,
            overwrite_a=overwrite_a, overwrite_b=overwrite_b,
            type=type, check_finite=check_finite,
            subset_by_index=subset_by_index, subset_by_value=subset_by_value,
            driver=driver)
```

docstring 也直言它「kept as a legacy convenience」（保留作遗留便利函数），并建议需要完全控制时直接用 `eigh`。

#### 4.2.3 源码精读

**签名**：

[_decomp.py:L971-L973](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L971-L973)：注意相比 `eigh`，`eigvalsh` 的签名**没有** `eigvals_only` 参数（因为它恒为 `True`），其余参数完全一致。

**实现**：

[_decomp.py:L1084-L1087](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1084-L1087)：纯粹的参数转发，没有任何额外计算。理解了 `eigh`，就理解了 `eigvalsh`。

#### 4.2.4 代码实践

**实践目标**：用 `eigvalsh` 判断矩阵正定性，并体会「只求一段特征值」的省时效果。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.linalg import eigvalsh

# 构造一个对称正定矩阵（A = M^T M 必然半正定，加扰动保证正定）
M = np.random.rand(6, 6)
A = M.T @ M + 0.1 * np.eye(6)

# 全部特征值
w_all = eigvalsh(A)
print("全部特征值:", w_all)
print("是否正定:", np.all(w_all > 0))   # 预期 True

# 只求最小的 3 个特征值（subset_by_index）
w_min3 = eigvalsh(A, subset_by_index=[0, 2])
print("最小 3 个:", w_min3)

# 验证：与全量结果的前 3 个一致
print("一致:", np.allclose(w_min3, w_all[:3]))  # 预期 True
```

**需要观察的现象**：

- `w_all` 全部为正，确认正定。
- `w_min3` 与 `w_all[:3]` 数值一致，但只计算了 3 个特征值（对大矩阵省时显著）。

**预期结果**：正定性判断为 `True`，`w_min3` 与全量前 3 个 `allclose`。

> 待本地验证：对几百阶以上的矩阵，可用 `%timeit` 对比 `eigvalsh(A)` 与 `eigvalsh(A, subset_by_index=[0,2])` 的耗时差异。

#### 4.2.5 小练习与答案

**练习 1**：既然 `eigvalsh` 只是 `eigh(..., eigvals_only=True)` 的简写，为什么不直接用 `eigh`？docstring 给出了什么建议？

> **参考答案**：docstring 说 `eigvalsh`「kept as a legacy convenience」，并建议「It might be beneficial to use the main function（即 `eigh`）to have full control and to be a bit more pythonic」。也就是说，新代码可以直接用 `eigh(..., eigvals_only=True)` 以获得更统一的接口和更明确的语义。

**练习 2**：`eigh` 的 docstring 提到「是否请求特征向量会影响底层算法，从而导致特征值结果略有不同」。这对 `eigvalsh` 意味着什么？

> **参考答案**：`eigvalsh` 固定 `eigvals_only=True`，因此它走的底层算法分支与 `eigh(..., eigvals_only=False)` 可能不同，得到的特征值在量级 `机器精度 × 最大特征值` 上可能有差异，主要影响接近 0 的特征值。也就是说，`eigvalsh(A)` 与 `eigh(A)` 的特征值**并非逐位相同**，但差异极小。

### 4.3 `_check_select` 与 subset 选择参数体系

#### 4.3.1 概念说明

`scipy.linalg` 里其实有**两套**「只求部分特征值」的 API，初学者容易混淆：

| API 风格 | 使用者 | 参数形态 |
| --- | --- | --- |
| **新式**：关键字 `subset_by_index` / `subset_by_value` | `eigh`、`eigvalsh` | 关键字参数，可直接传 `[1, 4]`，0 基索引 |
| **旧式**：位置/关键字 `select` + `select_range` | `eig_banded`、`eigvals_banded`、`eigh_tridiagonal`、`eigvalsh_tridiagonal` | `select` 取 `'a'`/`'v'`/`'i'`（或 `0/1/2`）字符串，配 `select_range` |

`_check_select` 正是**旧式 API** 的校验器：它把 `select` 字符串翻译成整数编码，把 `select_range` 翻译成 LAPACK 风格的 `vl/vu/il/iu`，并算出最多返回的特征值个数 `max_ev`。

> 重要：`_check_select` **不被 `eigh` 使用**。`eigh` 的 subset 校验是内联在第 522-545 行完成的（见 4.1.3）。`_check_select` 服务的是带状/三对角家族。本讲之所以要讲它，是因为它是 `scipy.linalg` 里「subset 选择」这一主题的另一半，且它把 Python 风格参数翻译成 Fortran 风格的套路与 `eigh` 如出一辙，对比阅读能加深理解。

#### 4.3.2 核心流程

```
_check_select(select, select_range, max_ev, max_len)
│
├─ 1. 把 select 归一化（小写字符串）后用 _conv_dict 映射为整数：
│       'all'/'a'/0 → 0（全部）
│       'value'/'v'/1 → 1（按数值区间）
│       'index'/'i'/2 → 2（按下标）
├─ 2. 默认 vl=0, vu=1, il=iu=1
├─ 3. 若 select != 0（非全部）：
│       - 校验 select_range 是 2 元素、非降序数组
│       - select==1（value）：vl, vu = select_range
│       - select==2（index）：要求整数 dtype，il, iu = select_range + 1（转 1 基），校验范围，max_ev = iu-il+1
└─ 返回 (select, vl, vu, il, iu, max_ev)
```

`_conv_dict` 是这套映射的核心数据：

```python
_conv_dict = {0: 0, 1: 1, 2: 2,
              'all': 0, 'value': 1, 'index': 2,
              'a': 0, 'v': 1, 'i': 2}
```

它同时接受整数 `0/1/2`、全称字符串 `'all'/'value'/'index'`、和首字母 `'a'/'v'/'i'` 三种等价写法。

#### 4.3.3 源码精读

**映射字典**

[_decomp.py:L650-L652](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L650-L652)：`_conv_dict` 把多种写法统一到整数编码 `0/1/2`。

**`_check_select` 实现**

[_decomp.py:L655-L685](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L655-L685)：逐段解析：

- 第 657-662 行：`select` 先 `lower()` 再查 `_conv_dict`，查不到抛 `ValueError('invalid argument for select')`。
- 第 665-669 行：非「全部」模式下，要求 `select_range` 是 2 元素、非降序的一维数组。
- 第 670-673 行（value 模式）：`vl, vu = select_range`，若调用方没指定 `max_ev` 则取 `max_len`（全部可能命中的个数）。
- 第 674-684 行（index 模式）：**强制要求 `select_range` 是整数 dtype**（第 675-679 行用 `dtype.char.lower() in 'hilqp'` 判定），否则报错；把 0 基转成 1 基（`il, iu = sr + 1`），校验范围，并算 `max_ev = iu - il + 1`。

**谁在调用 `_check_select`**

它被带状/三对角家族调用，例如：

[_decomp.py:L821](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L821)：`eig_banded` 内部 `select, vl, vu, il, iu, max_ev = _check_select(...)`。

[_decomp.py:L1369](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1369)：`eigh_tridiagonal` 内部同样调用 `_check_select`。

这两个函数及其 driver 选择逻辑是下一讲（u4-l3）的主题。

**与 `eigh` subset API 的对照**

`eigh` 的 subset 校验（[_decomp.py:L522-L545](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L522-L545)）做的事和 `_check_select` 高度相似——都是把人类友好的「下标/区间」翻译成 LAPACK 的 `range/il/iu/vl/vu`，并做范围校验。区别在于：

- `eigh` 用两个独立关键字 `subset_by_index`、`subset_by_value`，互斥（不能同时给）。
- `_check_select` 用一个 `select` 字符串指定「模式」，再用 `select_range` 给具体范围。

#### 4.3.4 代码实践

**实践目标**：对比同一矩阵用 `eigh` 的两种 subset API（`subset_by_index` 与 `subset_by_value`），直观感受「按下标」与「按数值区间」的差异。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.linalg import eigh

# 构造对称矩阵
B = np.random.rand(6, 6)
A = B + B.T

# 方式一：subset_by_index（0 基下标）—— 严格返回最小的 3 个
w_idx = eigh(A, eigvals_only=True, subset_by_index=[0, 2])

# 方式二：subset_by_value（数值半开区间 (lo, hi]）—— 命中个数取决于区间
# 先看全量特征值范围，再设一个区间
w_all = eigh(A, eigvals_only=True)
lo, hi = w_all[0], w_all[2] + 1e-9   # 覆盖最小的 3 个
w_val = eigh(A, eigvals_only=True, subset_by_value=[lo, hi])

print("全量:",        np.round(w_all, 4))
print("subset_by_index 最小3个:", np.round(w_idx, 4))
print("subset_by_value 命中:",    np.round(w_val, 4))
```

**需要观察的现象**：

- `subset_by_index=[0,2]` **严格**返回最小的 3 个特征值，长度恒为 3。
- `subset_by_value` 按数值半开区间 \((lo, hi]\) 过滤，命中的个数取决于区间内实际有多少特征值。当区间边界恰好贴近某个特征值时，是否包含取决于 LAPACK 的开闭约定（区间为 \((lo, hi]\)，左开右闭）。

**预期结果**：本例中 `w_idx` 与 `w_val` 数值一致（都覆盖最小的 3 个），但语义不同——前者按位次、后者按数值。

> 待本地验证：完整的带状旧式 `select='i'` 演示需要先正确构造带状压缩存储格式（承接 u2-l4）。建议学完 u4-l3 带状特征值后，再对比 `eig_banded(Ab, select='i', select_range=[1, 3])`（注意这里是 1 基）与 `eigh(A, subset_by_index=[0, 2])`（0 基），体会两套 API 的下标基差异。

#### 4.3.5 小练习与答案

**练习 1**：`_check_select` 中，为什么 `select='i'`（index 模式）要强制 `select_range` 是整数 dtype？

> **参考答案**：因为 index 模式表示的是下标，必须用整数；若是浮点数，则语义不清（下标 2.5 无意义），且 LAPACK 的 `il/iu` 也是整数。源码用 `dtype.char.lower() in 'hilqp'` 判定是否为整数类型，非整数直接抛 `ValueError`（见 [_decomp.py:L675-L679](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L675-L679)）。

**练习 2**：`_conv_dict` 为什么要同时接受 `'all'`、`'a'`、`0` 三种写法？

> **参考答案**：为了向后兼容和调用便利。LAPACK 习惯用字符 `'A'/'V'/'I'`，Python 用户可能习惯写整数 `0/1/2`，也有人喜欢简写首字母。`_conv_dict` 把这三种风格统一映射到内部整数编码，让不同风格的调用者都能用。

## 5. 综合实践

设计一个贯穿本讲的任务：**构造一个对称矩阵，用 `eigh` 只求最大 3 个特征值与特征向量，并与全量结果对比。**

```python
import numpy as np
from scipy.linalg import eigh

np.random.seed(0)
n = 8
B = np.random.rand(n, n)
A = B + B.T                       # 对称矩阵

# 1) 全量分解（基准）
w_all, v_all = eigh(A)

# 2) 只求最大的 3 个（注意 eigh 返回升序，最大 3 个的下标是 [n-3, n-1]）
w_top, v_top = eigh(A, subset_by_index=[n-3, n-1])

print("全量特征值:", np.round(w_all, 4))
print("最大 3 个:",   np.round(w_top, 4))
print("与全量末尾 3 个一致:", np.allclose(w_top, w_all[-3:]))

# 3) 验证特征向量：A v ≈ λ v
for i in range(3):
    res = np.linalg.norm(A @ v_top[:, i] - w_top[i] * v_top[:, i])
    print(f"第 {i} 个特征向量残差: {res:.2e}")

# 4) 验证正交性
print("正交性误差:", np.linalg.norm(v_top.T @ v_top - np.eye(3)))

# 5) 试用 subset_by_value：求落在 (0, +inf) 内的特征值
w_pos = eigh(A, eigvals_only=True, subset_by_value=[0, np.inf])
print("正特征值个数:", w_pos.size, np.round(w_pos, 4))
```

**完成标准**：

- `w_top` 与 `w_all[-3:]` 数值一致。
- 3 个特征向量的残差都在 \(10^{-14}\) 量级。
- `v_top` 是列正交的（`v_top.T @ v_top ≈ I`）。
- `w_pos` 的个数等于 `w_all` 中正特征值的个数。

**进阶**（选做）：把上述矩阵也写成广义问题形式 \(A v = \lambda I v\)（即传 `b=np.eye(n)`），用 `eigh(A, b)` 求解，对比标准问题的结果；并故意构造一个不正定的 `b`（如 `b = np.diag([1,1,1,-1,...])`），观察 `info>n` 触发的 `LinAlgError`「B is not positive definite」。

## 6. 本讲小结

- `eigh` 是对称/Hermitian 特征值问题的专用入口，相比 `eig`：返回**实特征值**、**正交特征向量**，更快更稳，还支持只算一部分。
- `eigh` 把人类友好参数翻译成 LAPACK 风格：`lower`→`uplo`、`eigvals_only`→`jobz/compute_v`、`subset_by_index`→`range='I'`+1 基 `il/iu`、`subset_by_value`→`range='V'`+`vl/vu`，前缀按实/复取 `sy`/`he`。
- driver 默认取最通用的 `evr`（标准）/`gvd`（广义全量）/`gvx`（广义子集）；`ev`/`evd`/`gv`/`gvd` 不支持 subset。
- `eigh` 的批处理靠 Python 层 `@_apply_over_batch` 装饰器逐片循环（每片走 f2py LAPACK），与 `eig` 的 C++ 后端原生批处理是两条不同路径。
- `eigvalsh` 是 `eigh(..., eigvals_only=True)` 的一行薄封装，docstring 自述为「legacy convenience」。
- `_check_select` 服务的是**带状/三对角**家族的旧式 `select`+`select_range` API，与 `eigh` 的新式 `subset_by_*` API 平行而不同源，但「翻译成 LAPACK `il/iu/vl/vu`」的套路完全一致。

## 7. 下一步学习建议

- **紧接 u4-l3（带状与三对角特征值问题）**：那里会讲 `eig_banded`、`eigvals_banded`、`eigh_tridiagonal`、`eigvalsh_tridiagonal`，它们正是 `_check_select` 的实际使用者，学完后你能把本讲的 subset 校验逻辑与它们的 `select` API 完整对应起来。
- **如果想看更底层的 LAPACK 分发**：可预习 u7-l1（`get_lapack_funcs` 与类型分发），理解本讲里 `pfx + driver` 是如何按 `s/d/c/z` 前缀最终落到一个具体 Fortran 例程上的。
- **矩阵函数方向**：如果对 `expm`/`sqrtm` 这类「对矩阵作用函数」更感兴趣，可跳到 u5；它们内部会反复用到本讲和 u3-l5 的 Schur/特征值分解。
