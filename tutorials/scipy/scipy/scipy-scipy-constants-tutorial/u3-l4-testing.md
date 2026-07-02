# 测试体系与回归保护

## 1. 本讲目标

`scipy.constants` 子包体量虽小，但它承载的是「全 SciPy 共享的物理常数真值」——一个常量值写错小数点，下游的物理、光学、热学计算就会整体偏移。因此它的测试不在「多」，而在「准」。

本讲学完后你应该能够：

- 看懂 `tests/test_codata.py` 与 `tests/test_constants.py` 两个测试文件各自负责什么、为什么用不同的断言风格。
- 理解「回归测试」（以 gh 编号命名的测试）如何把一个历史 bug 永久钉死，防止它再次复现。
- 掌握 `make_xp_test_case` 装饰器如何读取 `@xp_capabilities` 的元数据，把同一个测试自动参数化到多个数组库（NumPy/CuPy/PyTorch/JAX）上运行。
- 能够自己动手运行测试套件，并新增一个验证 `precision()` 定义的小测试。

本讲是专家篇的收尾，需要你已读过 u2-l3（精确值回填机制）和 u3-l1（`convert_temperature` 的实现），因为本讲大量断言就是在锁定那两讲描述的行为。

## 2. 前置知识

阅读本讲前，先用通俗语言理清三个概念：

1. **回归测试（regression test）**：当某个 bug 被修复后，专门写一条「触发该 bug 的最小用例」并固定下来。以后任何人改动代码，只要这条用例还能通过，就说明 bug 没有复活。SciPy 惯例用 `test_gh<编号>` 命名，编号对应 GitHub issue/PR，便于溯源。

2. **断言（assertion）**：测试的核心动作就是「断言实际结果等于期望结果」。本子包里出现两套断言：
   - `numpy.testing.assert_equal / assert_` ：经典断言，只认 NumPy 数组与 Python 标量。
   - `xp_assert_equal / xp_assert_close` ：「数组 API 感知」断言，能同时校验「数值对」「命名空间对（是不是同一个数组库）」「dtype 对」「shape 对」。

3. **测试夹具 fixture**：pytest 里以参数注入测试函数的对象。本讲的 `xp` 就是一个夹具，它代表「当前这一轮测试使用的数组命名空间」（如 `numpy`、`cupy`）。同一个测试函数被它参数化后，会针对每个已安装的数组库各跑一遍。

如果你对 CODATA 数据库结构（`physical_constants` 三元组、`value/unit/precision/find`）还不熟，请先回顾 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [scipy/constants/tests/test_codata.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py) | 针对 CODATA 数据库的测试：查找 API、基础解析、精确值回填、两个历史 bug 的回归测试。全部用经典 `numpy.testing` 断言。 |
| [scipy/constants/tests/test_constants.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py) | 针对 `convert_temperature` / `lambda2nu` / `nu2lambda` 三个数组函数的测试，用 `make_xp_test_case` 把它们参数化到多个数组库。 |
| [scipy/constants/tests/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/meson.build) | 把三个测试源文件注册到 Meson 构建，并以 `install_tag: 'tests'` 标记，安装时可单独排除。 |
| scipy/constants/_codata.py | 被测对象：`physical_constants` 字典、`value/unit/precision/find`、`exact2018`、`replace_exact`。 |
| scipy/constants/_constants.py | 被测对象：`convert_temperature`、`lambda2nu`、`nu2lambda`，三者均戴 `@xp_capabilities()` 装饰器。 |
| scipy/_lib/_array_api.py（外部模块） | 提供 `make_xp_test_case`、`xp_assert_equal`、`xp_assert_close` 等跨后端测试基础设施。 |
| scipy/_lib/_array_api_no_0d.py（外部模块） | `test_constants.py` 实际导入的断言变体，额外禁止「0 维数组」作为返回值，强制遵循 NumPy 的标量返回约定。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：查找与基础解析测试、精确值与回归测试、数组 API 测试与 `make_xp_test_case`、跨后端断言工具。

### 4.1 CODATA 查找与基础解析测试

#### 4.1.1 概念说明

`test_codata.py` 覆盖的是「数据本身是否正确装载」这一层。它要回答三个问题：

- `find()` 能否按子串（大小写不敏感）找到正确的键集合？
- 文本解析出来的数值，是否和直接暴露的顶层常量（如 `c`、`speed_of_light`）完全一致？
- 数据库规模是否合理（不是几十条，也不是几万条）？

这一层测试都是纯 Python 标量与字典操作，没有任何数组运算，所以文件顶部只导入了经典的 `numpy.testing` 断言：

[test_codata.py:L1-L4](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L1-L4) —— 用 `assert_equal / assert_ / assert_almost_equal` 做断言，并 `import scipy.constants._codata as _cd` 以访问私有解析产物（如 `_physical_constants_2018`、`exact2018`）。

#### 4.1.2 核心流程

`find` 相关测试的思路是「给定子串 → 断言返回的键列表精确等于预期」：

- `test_find` 一次性覆盖三种情形：唯一命中（`'weak mixing'`）、零命中（`'qwertyuiop'` → 空列表）、多命中且需排序（`'natural unit'`）。
- `test_find_all` 不传子串，断言「全量键数 > 300」，间接守住数据库规模。
- `test_find_single` 验证缩写子串 `'Wien freq'` 能定位到完整键名。
- `test_basic_table_parse` 与 `test_basic_lookup` 把「文本解析值」与「顶层暴露常量」对齐：`value('speed of light in vacuum')` 必须等于 `c` 也等于 `speed_of_light`。

#### 4.1.3 源码精读

[test_codata.py:L7-L24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L7-L24) —— `test_find`。注意第三组用 `sorted([...])`：`find` 内部对结果排序，测试也排序后再比，保证顺序无关。第二组 `'qwertyuiop'` 断言返回 `[]`，锁定了「无命中时返回空列表而非 None」的行为。

[test_codata.py:L39-L45](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L39-L45) —— `test_find_all` 与 `test_find_single`。`find(disp=False)` 不传 `sub` 即返回当前数据集（CODATA 2022）的全部键，`assert_(... > 300)` 是一条「规模下限」护栏。

[test_codata.py:L27-L36](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L27-L36) —— `test_basic_table_parse` / `test_basic_lookup`。前者断言 `value('speed of light in vacuum') == c == speed_of_light`，把「文本解析产物」与「`_constants.py` 里的 `c = 299792458.0` 字面量」绑成同一个数；后者还顺带校验了单位字符串 `'299792458 m s^-1'`。

这些断言之所以成立，根源在 `_codata.py` 里 `find` 与 `value` 的实现：

- [find: _codata.py:L2207-L2208](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2207-L2208) 只在 `_current_constants`（即 CODATA 2022）里做大小写不敏感子串匹配，所以旧版独有键不会出现在 `find` 结果里。
- [value: _codata.py:L2129-L2152](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2129-L2152) 先 `_check_obsolete(key)` 再取 `physical_constants[key][0]`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `find` 的三类返回与「全量规模」。
2. **操作步骤**：在已安装 SciPy 的环境里运行
   ```python
   from scipy.constants import find, value, c, speed_of_light
   print(find('weak mixing', disp=False))      # 期望 ['weak mixing angle']
   print(find('qwertyuiop', disp=False))       # 期望 []
   print(len(find(disp=False)))                # 期望 > 300
   print(value('speed of light in vacuum') == c == speed_of_light)  # 期望 True
   ```
3. **需要观察的现象**：`find` 返回的是「键名列表」而非值；不传 `sub` 返回全部当前键。
4. **预期结果**：四行依次输出 `['weak mixing angle']`、`[]`、一个大于 300 的整数、`True`。
5. **待本地验证**：第二条返回的整数具体数值取决于当前 CODATA 版本，本讲只保证它 > 300。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test_find` 里对 `'natural unit'` 的期望列表要包一层 `sorted(...)`？
**答案**：`find` 内部对结果做了排序，测试端也排序后比较，可以避免「列表元素相同但顺序不同」造成的假失败，让断言只关心「集合是否相等」。

**练习 2**：如果某个常量只存在于 CODATA 2002、在 2022 里已被删除，`find('它的名字')` 会返回什么？
**答案**：返回空列表 `[]`。因为 `find` 只在 `_current_constants`（CODATA 2022）中搜索，废弃键不在当前集里。要拿到它的旧值必须直接用 `value()`（会触发 `ConstantWarning`）。

---

### 4.2 精确值回归与历史 bug 锁定（gh-11341 / gh-14467）

#### 4.2.1 概念说明

这一组测试是本讲的精华：它们不是「随手写的用例」，而是「真实发生过的 bug 被修复后留下的永久证据」。

- **gh-11341**：有人报告「真空电容率」有好几个等价名字（`epsilon_0`、`electric constant`、`vacuum electric permittivity`），它们必须始终相等。这个 bug 关注的是**别名兼容性**。
- **gh-14467**：有人发现 CODATA 文本里某些「本应精确」的常数被截断到了约 10 位有效数字（如 `Boltzmann constant in eV/K`），损失了双精度应有的精度。这个 bug 关注的是**精确值回填**（u2-l3 讲过的 `replace_exact` 机制）。

把这两个 bug 写成测试，意味着：将来任何人重写解析器或别名逻辑，只要这两条测试还过，就说明这两个坑没有被重新踩进去。

#### 4.2.2 核心流程

- `test_exact_values`：直接调用私有函数 `_cd.exact2018(exact)` 重新算一遍全部精确派生值，再逐一断言「回填后的库里 `value(key)` 等于推导值」且「`precision(key) == 0`」（精确常数相对精度为 0）。
- `test_gh11341`：取同一物理量的三个不同名字，断言三者值相等。
- `test_gh14467`：用「Boltzmann 常数 ÷ 元电荷」手算 `k/e`，断言它等于库里现存的 `Boltzmann constant in eV/K`，证明截断已被修复。

其中 `precision` 的定义是相对不确定度：

\[
\mathrm{precision}(key) = \frac{\mathrm{uncertainty}(key)}{\mathrm{value}(key)}
\]

对精确常数，不确定度为 0，故 precision 为 0。

#### 4.2.3 源码精读

[test_codata.py:L53-L59](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L53-L59) —— `test_exact_values`。它从 `_cd._physical_constants_2018` 构造 `exact` 字典（只取每条三元组的 value 作为推导原料），喂给 `_cd.exact2018(exact)` 得到 `replace`，再遍历断言两条：`val == value(key)`（回填值一致）与 `precision(key) == 0`（精确常数无不确定度）。这条测试等于把 u2-l3 的整套回填机制端到端复跑一遍。

[test_codata.py:L62-L68](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L62-L68) —— `test_gh11341`。`constants.epsilon_0`、`physical_constants['electric constant'][0]`、`physical_constants['vacuum electric permittivity'][0]` 三者必须相等。这能成立，是因为 `_codata.py` 末尾的别名表把 `electric constant` 重定向到了 `vacuum electric permittivity`（见 [_aliases: _codata.py:L2094-L2096](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2094-L2096)），而 `epsilon_0` 又是该值的顶层别名。

[test_codata.py:L71-L78](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L71-L78) —— `test_gh14467`。它用「Boltzmann 常数 ÷ 元电荷」作为参考值 `ref`，断言库里的 `Boltzmann constant in eV/K` 与之**严格相等**（`==`，不是近似）。这正是 `exact2018` 中 `'Boltzmann constant in eV/K': k / e`（[exact2018: _codata.py:L1564-L1566](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1564-L1566)）经 `replace_exact` 回填后的结果。修复前文本里是 `... eV/K`（被截断），`==` 会失败；修复后用全精度推导值替换，`==` 成立。

注意 `replace_exact` 只替换三元组的 value，保留原 unit 与 uncertainty（[replace_exact: _codata.py:L2047-L2053](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2047-L2053)），而精确常数的 uncertainty 在解析时已由 `(exact)` 标记记为 `0.0`，所以 `precision(key) == 0`。

#### 4.2.4 代码实践

1. **实践目标**：复现 gh-14467 的「手算 vs 库值」对照，理解 `==` 为何能成立。
2. **操作步骤**：
   ```python
   from scipy import constants
   res = constants.physical_constants['Boltzmann constant in eV/K'][0]
   ref = (constants.physical_constants['Boltzmann constant'][0]
          / constants.physical_constants['elementary charge'][0])
   print(res, ref, res == ref)   # 期望严格相等
   ```
3. **需要观察的现象**：两个值打印出来完全相同，`res == ref` 为 `True`。
4. **预期结果**：`True`。若你把 `exact2018` 里 `'Boltzmann constant in eV/K': k/e` 这行删掉重装，`res` 会退回被截断的文本值，`==` 就会变 `False`——这就是 gh-14467 复现的方式。
5. **待本地验证**：如果你想亲手复现 bug，需要修改源码并重新编译 SciPy，属于进阶操作，本讲不强制。

#### 4.2.5 小练习与答案

**练习 1**：`test_gh14467` 用的是 `assert res == ref`（严格相等），而不是 `assert_almost_equal`。为什么这里敢于用严格相等？
**答案**：因为 `res` 本身就是 `replace_exact` 用 `k/e` 推导出的全精度值回填的，与测试里手算的 `ref = k/e` 走的是同一套数学关系、同样的双精度运算，理论上位位相同。如果用近似相等，反而会放过「截断到 10 位」这种精度损失 bug。

**练习 2**：`test_exact_values` 里为什么断言 `precision(key) == 0` 而不是断言「值等于某个写死的数字」？
**答案**：写死数字会随 CODATA 版本变化而失效，维护成本高；而「精确常数的相对不确定度必为 0」是一条与具体数值无关的不变量，更稳定，也直接刻画了「精确」这个语义。

---

### 4.3 数组 API 测试与 `make_xp_test_case`

#### 4.3.1 概念说明

`test_constants.py` 测试的是三个「数组进、数组出」的函数：`convert_temperature`、`lambda2nu`、`nu2lambda`。它们都戴了 `@xp_capabilities()` 装饰器（见 u3-l2），意思是「我支持 Array API 标准，可以在多个数组库上跑」。

测试这些函数的难点在于：同一个 `convert_temperature`，输入可能是 NumPy 数组、CuPy 数组、PyTorch 张量……如果给每个后端写一份独立的测试，代码会爆炸。SciPy 的解法是 `make_xp_test_case`：**写一份测试，让框架根据函数的 `@xp_capabilities` 元数据，自动给每个后端打上 skip/xfail 标记，并配合 `xp` 夹具把同一份测试参数化到多个数组库。**

#### 4.3.2 核心流程

整体链路是这样的：

1. 你写一个普通测试类，方法签名带 `xp` 参数（如 `def test_xxx(self, xp)`），方法体里用 `xp.asarray(...)` 构造输入。
2. 在类上贴 `@make_xp_test_case(sc.convert_temperature)`。
3. `make_xp_test_case` 读取 `sc.convert_temperature` 上的 `@xp_capabilities` 元数据，生成一组 `pytest.mark.skip_xp_backends(...)` / `pytest.mark.xfail_xp_backends(...)` 标记，叠加到类的每个测试方法上。
4. 运行时，Array API 测试插件注入 `xp` 夹具，按「已安装的数组库」逐个 parametrize；对不支持的后端，前面打的标记会让它 skip 或 xfail。

关键在于：**标记的内容（哪些后端该跳过、该预期失败）不是手写的，而是从被测函数自己声明的 `@xp_capabilities` 能力表里自动推导出来的**——这就保证了「函数声明支持什么」与「测试覆盖什么」不会脱节。

#### 4.3.3 源码精读

[test_constants.py:L1-L7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L1-L7) —— 导入跨后端断言 `xp_assert_equal / xp_assert_close`（注意来自 `_array_api_no_0d`，见 4.4）、`make_xp_test_case`，并声明 `lazy_xp_modules = [sc]`，把 `scipy.constants` 模块注册进「延迟 xp 追踪」机制，使其内部 `@xp_capabilities` 装饰的函数被测试基础设施发现。

[test_constants.py:L10-L62](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L10-L62) —— `TestConvertTemperature`。类装饰器 `@make_xp_test_case(sc.convert_temperature)` 把 `convert_temperature` 的能力元数据翻译成 pytest 标记。`test_convert_temperature(self, xp)` 用 `xp.asarray(...)` 在「当前后端」上构造输入，覆盖 C/K/F/R 四温标的两两转换，冰点 0 °C = 273.15 K = 32 °F = 491.67 °R 作为锚点。`test_convert_temperature_array_like` 传 Python 列表（非 `xp.asarray`），验证「类数组输入」也能工作。`test_convert_temperature_errors` 用 `pytest.raises(NotImplementedError, match="old_scale=")` / `match="new_scale=")` 锁定不支持温标时的报错前缀（对应 u3-l1 讲过的两个 `else` 分支）。

[test_constants.py:L65-L83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L65-L83) —— `TestLambdaToNu` 与 `TestNuToLambda`，分别贴 `@make_xp_test_case(sc.lambda2nu)` 与 `@make_xp_test_case(sc.nu2lambda)`。光学函数的互逆关系被巧妙利用：`lambda2nu([c, 1]) == [1, c]`，`nu2lambda([c, 1]) == [1, c]`（因为 `nu = c/lambda`，当 `lambda=c` 时 `nu=1`，反之亦然）。

装饰器本身定义在外部模块 [_array_api.py:L942-L1006](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L942-L1006)：`make_xp_test_case` 调用 `make_xp_pytest_marks(*funcs)` 生成标记列表，再用 `functools.reduce` 把它们逐层包到被装饰函数上。其 docstring 明确说明：它读取 `@xp_capabilities` 装饰器登记的参数，生成对应的 `skip_xp_backends` / `xfail_xp_backends`，并给函数打上 `lazy_xp_function` 标签。标记的真正来源在 [make_xp_pytest_marks: _array_api.py:L1083-L1142](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L1083-L1142)，它从能力表里取出 `cpu_only`、`exceptions`、`reason` 等字段，按规则翻译成 pytest 标记。

#### 4.3.4 代码实践

1. **实践目标**：观察 `make_xp_test_case` 如何让同一测试在多个后端上「各跑一遍」。
2. **操作步骤**：在仓库根目录运行
   ```bash
   python -m pytest scipy/constants/tests/test_constants.py -v
   ```
3. **需要观察的现象**：测试输出里，每个 `test_convert_temperature` / `test_lambda_to_nu` / `test_nu_to_lambda` 后面会带有后端标识（如 `[numpy]`）。若你额外装了 `array-api-compat` 与 CuPy/PyTorch 并设了 `SCIPY_ARRAY_API=1`，会看到同一测试出现多行（每个后端一行），未支持的后端显示 `SKIP` 或 `XFAIL`。
4. **预期结果**：在仅装 NumPy 的环境里，三个测试类的核心方法全部通过，`xp` 即 `numpy` 命名空间。
5. **待本地验证**：多后端的实际 skip/xfail 行为依赖你本地安装的数组库与 `SCIPY_ARRAY_API` 开关，本讲无法替你预判具体输出。

#### 4.3.5 小练习与答案

**练习 1**：如果某天 `convert_temperature` 新增了对 CuPy 的一个已知缺陷（预期失败），你需要改测试代码去手写 `@pytest.mark.xfail_xp_backends('cupy')` 吗？
**答案**：不需要。应该在 `_constants.py` 里 `convert_temperature` 的 `@xp_capabilities(...)` 装饰器参数中声明该缺陷，`make_xp_test_case` 会自动把它翻译成对应的 xfail 标记。这就是「单一数据源」：能力声明与测试标记同源。

**练习 2**：为什么 `test_convert_temperature_errors` 要分两条 `pytest.raises`，分别匹配 `old_scale=` 和 `new_scale=`？
**答案**：因为 `convert_temperature` 有两个独立的 `else` 分支——一个在「old_scale→Kelvin」阶段、一个在「Kelvin→new_scale」阶段，报错信息分别带 `old_scale=` 与 `new_scale=` 前缀（见 u3-l1）。分两条测试可以分别锁定两个方向的错误路径，互不掩盖。

---

### 4.4 跨后端断言工具 `xp_assert_equal` / `xp_assert_close` 与 `_array_api_no_0d`

#### 4.4.1 概念说明

`test_constants.py` 顶部导入的不是 `numpy.testing`，而是 `scipy._lib._array_api_no_0d` 里的 `xp_assert_equal / xp_assert_close`。要理解这个选择，得先明白两件事：

1. **为什么要用 `xp_` 版断言？** 经典 `numpy.testing.assert_array_equal` 只认 NumPy。如果要校验一个 CuPy 数组，它可能直接报错或做错误的类型推断。`xp_assert_*` 内部先做 `_strict_check`，统一校验「命名空间、dtype、shape、0 维性」四项，再按后端分发到各自的断言实现。

2. **为什么用 `_no_0d` 这个变体？** NumPy 在「标量 vs 0 维数组」上长期不一致——例如 `np.array(0) * 2` 返回的是 Python 标量而不是 0 维数组（这是 NumPy 的历史包袱）。而 CuPy 等库在同类场景下倾向于返回 0 维数组。`scipy.constants` 的三个函数遵循 NumPy 约定（标量输入返回标量），`_no_0d` 变体就用一条额外检查强制「不允许返回 0 维数组」，把这种约定钉死。

#### 4.4.2 核心流程

`xp_assert_equal(actual, desired)`（no_0d 变体）的处理顺序：

1. 若 `check_0d=False`（默认），先调 `_check_scalar`：当目标是 NumPy 且 `desired.shape == ()` 时，断言 `actual` 是标量（`xp.isscalar(actual)`），否则抛出带提示的断言错误。
2. 再调基础版 `xp_assert_equal_base`，它先 `_strict_check` 统一四项检查，再按后端分发：CuPy 走 `xp.testing.assert_array_equal`、PyTorch 走 `assert_close(rtol=0, atol=0)`、NumPy/JAX 走 `np.testing.assert_array_equal`。

`xp_assert_close` 同理，只是允许容差（`rtol`/`atol`），默认 `rtol` 对浮点取 `eps**0.5 * 4`。

#### 4.4.3 源码精读

[_array_api_no_0d.py:L1-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_no_0d.py#L1-L26) —— 模块 docstring 用 `np.array(0) * 2` 等例子直接点明 NumPy 的标量/0 维不一致，并说明本模块的策略是「禁止 0 维数组作为返回类型」。

[_array_api_no_0d.py:L35-L67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_no_0d.py#L35-L67) —— `_check_scalar` 与 no_0d 版 `xp_assert_equal`。`_check_scalar` 在「NumPy 且 desired 为 0 维」时断言 `xp.isscalar(actual)`；`xp_assert_equal` 默认 `check_0d=False`，先做标量检查再委托给 `xp_assert_equal_base`。

[_array_api.py:L278-L329](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L278-L329) —— 基础版 `xp_assert_equal` / `xp_assert_close`。二者都先 `_strict_check`，再按 `is_cupy(xp)` / `is_torch(xp)` 分发到对应库的原生断言，其余（NumPy/JAX）走 `np.testing`。`xp_assert_close` 对浮点默认 `rtol = eps**0.5 * 4`。

回到 `test_constants.py`，可以清楚看到两种断言的用法分工：

- 数值上「应当严格相等」的转换（如 32 °F → 0 °C）用 `xp_assert_equal`（[test_constants.py:L12-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L12-L26)）。
- 涉及浮点舍入（如 Rankine 换算 `* 5/9`）用 `xp_assert_close` 并显式给 `rtol=0., atol=1e-13`（[test_constants.py:L27-L51](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L27-L51)），既要求几乎严格相等，又容忍浮点末位误差。

#### 4.4.4 代码实践

1. **实践目标**：体会 `xp_assert_close` 的容差参数如何工作。
2. **操作步骤**：
   ```python
   import scipy.constants as sc
   from scipy._lib._array_api_no_0d import xp_assert_close
   import numpy as np
   # Rankine -> Kelvin: 491.67 R == 273.15 K
   xp_assert_close(sc.convert_temperature(np.array([491.67, 0.]), 'rankine', 'kelvin'),
                   [273.15, 0.], rtol=0., atol=1e-13)
   print("ok")
   ```
3. **需要观察的现象**：若把 `atol` 调到极小（如 `1e-20`），断言可能因浮点末位差异而失败。
4. **预期结果**：上述代码打印 `ok`；缩小 `atol` 后会抛出 `AssertionError` 并打印实际与期望值的差异。
5. **待本地验证**：浮点末位的具体差异依赖平台与 NumPy 版本。

#### 4.4.5 小练习与答案

**练习 1**：`test_convert_temperature` 里 32 °F → 0 °C 用的是 `xp_assert_equal`，而 0 °C → 491.67 °R 用的是 `xp_assert_close(rtol=0, atol=1e-13)`。为什么后者不能用 `xp_assert_equal`？
**答案**：摄氏→兰氏的公式是 `(T + 273.15) * 9/5`，含 `9/5` 与 273.15 的浮点运算，结果在双精度下可能落在 `491.6700000000001` 之类末位抖动上，严格 `==` 会偶发失败。`rtol=0, atol=1e-13` 等价于「几乎严格相等」，排除了这种末位噪声。而 32 °F → 0 °C 的运算 `(32-32)*5/9` 结果恰好是干净的 0.0，可以严格相等。

**练习 2**：如果有一天 `convert_temperature` 被改成对 0 维输入返回 0 维数组（而非标量），哪条测试会先红？
**答案**：`test_convert_temperature` 里所有 `xp_assert_equal(sc.convert_temperature(xp.asarray(32.), ...), xp.asarray(0.0))` 这类用例会红，因为 no_0d 版 `_check_scalar` 会断言 `actual` 必须是标量，0 维数组会被拒绝并打印「Result is a NumPy 0d-array ...」的提示。这正是 `_no_0d` 变体的护栏作用。

---

### 4.5 测试如何被构建与安装（tests/meson.build）

最后补一块容易被忽略的「胶水」：测试文件本身如何进入构建系统。

[tests/meson.build:L1-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/meson.build#L1-L12) 列出 `__init__.py`、`test_codata.py`、`test_constants.py` 三个源文件，通过 `py3.install_sources(..., install_tag: 'tests')` 安装到 `scipy/constants/tests/` 子目录。

要点：

- `install_tag: 'tests'` 是个标签，让发行版打包者可以「只装库、不装测试」（例如用 `meson install --tags runtime` 跳过测试），减小生产环境体积。
- `__init__.py` 也在列表里，使 `scipy.constants.tests` 成为一个合法的 Python 子包，`python -m pytest scipy/constants/tests` 才能正常发现用例。
- 这与 u1-l2 讲过的「构建树与源码树同构、`subdir` 挂载、`install_tag` 区分用途」完全一致，测试目录只是同一套机制的又一个实例。

## 5. 综合实践

把本讲的「运行测试 + 新增测试 + 理解参数化」串起来，完成下面这个小任务。

**任务**：为 `precision()` 的定义加一条回归测试，并解释它如何随 `make_xp_test_case` 在多后端运行。

**步骤**：

1. 在仓库根目录运行整套 constants 测试，确认基线全绿：
   ```bash
   python -m pytest scipy/constants/tests -v
   ```
2. 新建一个临时测试文件（不要改动 SciPy 源码，只是练手），内容如下：
   ```python
   # 文件名示例：test_precision_def.py（练习用，非项目正式测试）
   from scipy.constants import physical_constants, precision

   def test_precision_is_uncertainty_over_value():
       key = 'Stefan-Boltzmann constant'   # 一个由 exact2018 推导的精确常数
       val, unit, uncert = physical_constants[key]
       assert precision(key) == uncert / val   # 精确常数两侧均为 0.0
       # 再取一个非精确常数，验证 precision 为正
       key2 = 'proton mass'
       v2, u2, unc2 = physical_constants[key2]
       assert precision(key2) == unc2 / v2
       assert precision(key2) > 0
   ```
3. 运行它：`python -m pytest test_precision_def.py -v`，观察是否通过。
4. 解释：为什么 `Stefan-Boltzmann constant` 的 `precision()` 是 0？因为它在 `exact2018` 的 replace 字典里（[exact2018: _codata.py:L1623](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1623)），经 `replace_exact` 回填全精度值后，uncertainty 保持为解析 `(exact)` 时记下的 `0.0`，故 `0.0 / val == 0.0`。
5. 回答思考题：上面的测试**没有**用 `make_xp_test_case`，因为它测的是 `precision()`（标量函数，`@xp_capabilities(out_of_scope=True)`，见 [_codata.py:L2181-L2204](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2181-L2204)），本就不参与数组 API。对比 `test_constants.py` 里的 `TestConvertTemperature`：它测的是数组函数，所以必须贴 `@make_xp_test_case(sc.convert_temperature)`，框架才会把同一测试参数化到多个数组库。

**预期结果**：步骤 3 测试通过；步骤 5 能清楚说出「标量函数不需要 `make_xp_test_case`，数组函数才需要」。

**待本地验证**：步骤 1 的具体测试条数与耗时依赖你的机器与已安装的可选依赖。

## 6. 本讲小结

- `scipy.constants` 的测试分两个文件、两种风格：`test_codata.py` 用经典 `numpy.testing` 断言覆盖数据查找/解析/精确值；`test_constants.py` 用 `xp_assert_*` + `make_xp_test_case` 覆盖三个数组函数。
- `test_exact_values` 端到端复跑了 u2-l3 的 `exact2018` + `replace_exact` 回填机制，断言「精确常数 value 一致且 precision 为 0」。
- `test_gh11341`（别名兼容）与 `test_gh14467`（精确值截断）是两条回归测试，分别把两个历史 bug 永久钉死，是「修复的证据」。
- `make_xp_test_case` 读取被测函数 `@xp_capabilities` 的能力元数据，自动生成 `skip_xp_backends` / `xfail_xp_backends` 标记，让一份测试随 `xp` 夹具参数化到多个数组库——「函数声明支持什么」与「测试覆盖什么」同源。
- `test_constants.py` 故意导入 `_array_api_no_0d` 的断言变体，用 `_check_scalar` 强制「标量输入必须返回标量」，把 NumPy 的标量返回约定锁死。
- `tests/meson.build` 用 `install_tag: 'tests'` 把测试注册进构建，支持「装库不装测试」的按标签安装。

## 7. 下一步学习建议

本讲是 scipy.constants 学习手册的最后一篇，子包本身已无更多源码可深挖。建议你接下来：

1. **横向对比**：去看 SciPy 其他子包（如 `scipy.special`、`scipy.interpolate`）的测试目录，观察它们如何使用同一套 `make_xp_test_case` / `xp_assert_*` 基础设施，巩固本讲对 Array API 测试的理解。可以重点读 `scipy/_lib/_array_api.py` 里 `make_xp_pytest_marks` 的完整实现（[L1083-L1142](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L1083-L1142)）。
2. **纵向动手**：尝试在一个真实函数上加 `@xp_capabilities(...)` 并配 `make_xp_test_case`，跑一次多后端测试（设置 `SCIPY_ARRAY_API=1` 并安装 `array-api-compat`），亲眼看看 skip/xfail 标记如何生效。
3. **回归意识**：今后遇到任何 SciPy bug 报告，习惯性去看对应 `test_gh<编号>` 测试，把它当作「这份代码不能退化到什么状态」的契约来读。
