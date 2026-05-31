"""
工具集 stub 的行为测试。

确保 STUB 实现满足:
- 加解密是逆操作
- 计算工具能消费密文、产出密文
- 解密计算结果与明文等价计算一致
"""

from __future__ import annotations

from client.tools import HELearn, HENumpy, HETorch, PandaSeal, ZFHE
from shared.contract import Operation


# ---------------------------------------------------------------------------
# zfhe 加解密
# ---------------------------------------------------------------------------


def test_zfhe_encrypt_decrypt_roundtrip(zfhe: ZFHE):
    plain = [{"month": "2024-01", "amount": 100}]
    cipher = zfhe.encrypt(plain)
    assert isinstance(cipher, bytes)
    assert zfhe.decrypt(cipher) == plain


def test_zfhe_file_roundtrip(tmp_path, zfhe: ZFHE):
    src = tmp_path / "data.txt"
    src.write_text("hello", encoding="utf-8")
    cipher_path = tmp_path / "data.cipher"
    zfhe.encrypt_file(src, cipher_path)

    out_path = tmp_path / "data.out.txt"
    zfhe.decrypt_file(cipher_path, out_path)
    assert out_path.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# pandaseal 分组聚合
# ---------------------------------------------------------------------------


def test_pandaseal_group_sum(zfhe: ZFHE, pandaseal: PandaSeal, sample_sales_rows):
    cipher_in = zfhe.encrypt(sample_sales_rows)
    ops = [
        Operation(op="group_by", field="month"),
        Operation(op="sum", field="amount"),
    ]
    cipher_out = pandaseal.run(ops, cipher_in)
    result = zfhe.decrypt(cipher_out)

    by_month = {r["__group__"]: r["amount_sum"] for r in result}
    assert by_month == {"2024-01": 300, "2024-02": 400, "2024-03": 300}


def test_pandaseal_mean(zfhe: ZFHE, pandaseal: PandaSeal, sample_sales_rows):
    cipher_in = zfhe.encrypt(sample_sales_rows)
    ops = [
        Operation(op="group_by", field="month"),
        Operation(op="mean", field="amount"),
    ]
    cipher_out = pandaseal.run(ops, cipher_in)
    result = zfhe.decrypt(cipher_out)
    by_month = {r["__group__"]: r["amount_mean"] for r in result}
    assert by_month["2024-01"] == 150
    assert by_month["2024-02"] == 200


def test_pandaseal_filter(zfhe: ZFHE, pandaseal: PandaSeal, sample_sales_rows):
    cipher_in = zfhe.encrypt(sample_sales_rows)
    ops = [Operation(op="filter", params={"field": "category", "op": "eq", "value": "A"})]
    cipher_out = pandaseal.run(ops, cipher_in)
    result = zfhe.decrypt(cipher_out)
    assert all(r["category"] == "A" for r in result)


# ---------------------------------------------------------------------------
# henumpy 数值
# ---------------------------------------------------------------------------


def test_henumpy_sum(zfhe: ZFHE, henumpy: HENumpy):
    cipher_in = zfhe.encrypt([1, 2, 3, 4])
    cipher_out = henumpy.run([Operation(op="sum")], cipher_in)
    assert zfhe.decrypt(cipher_out) == 10.0


def test_henumpy_dot(zfhe: ZFHE, henumpy: HENumpy):
    cipher_in = zfhe.encrypt({"a": [1, 2, 3], "b": [4, 5, 6]})
    cipher_out = henumpy.run([Operation(op="dot")], cipher_in)
    assert zfhe.decrypt(cipher_out) == 32


def test_henumpy_corrcoef(zfhe: ZFHE, henumpy: HENumpy):
    cipher_in = zfhe.encrypt({"x": [1, 2, 3, 4], "y": [2, 4, 6, 8]})
    cipher_out = henumpy.run([Operation(op="corrcoef")], cipher_in)
    result = zfhe.decrypt(cipher_out)
    assert result["labels"] == ["x", "y"]
    # 完全线性相关 → 接近 1.0
    assert abs(result["matrix"][0][1] - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# helearn 推理
# ---------------------------------------------------------------------------


def test_helearn_linreg(zfhe: ZFHE, helearn: HELearn):
    X = [[1, 2], [3, 4]]
    cipher_in = zfhe.encrypt(X)
    ops = [Operation(op="linear_regression_predict", params={"weights": [1, 1], "bias": 0})]
    cipher_out = helearn.run(ops, cipher_in)
    result = zfhe.decrypt(cipher_out)
    assert result == [3, 7]


def test_helearn_kmeans(zfhe: ZFHE, helearn: HELearn):
    X = [[0, 0], [10, 10]]
    cipher_in = zfhe.encrypt(X)
    ops = [Operation(op="kmeans_predict", params={"centroids": [[0, 0], [10, 10]]})]
    cipher_out = helearn.run(ops, cipher_in)
    assert zfhe.decrypt(cipher_out) == [0, 1]


# ---------------------------------------------------------------------------
# hetorch 推理
# ---------------------------------------------------------------------------


def test_hetorch_classify(zfhe: ZFHE, hetorch: HETorch):
    X = [[1, 2, 3]]
    cipher_in = zfhe.encrypt(X)
    ops = [Operation(op="classify", params={"model_id": "stub", "n_classes": 3})]
    cipher_out = hetorch.run(ops, cipher_in)
    result = zfhe.decrypt(cipher_out)
    assert len(result) == 1
    assert "label" in result[0]
    assert "confidence" in result[0]
