package main

import (
	"bytes"
	"math/big"
	"testing"

	"github.com/consensys/gnark-crypto/ecc/bn254/fr"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/polynomial"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

func TestAuthenticatedPolynomialOpening(t *testing.T) {
	data := polynomial.MultiLin{fr.NewElement(3), fr.NewElement(5), fr.NewElement(7), fr.NewElement(11),
		fr.NewElement(13), fr.NewElement(17), fr.NewElement(19), fr.NewElement(23)}
	rows, err := commitRows(data, 4)
	if err != nil {
		t.Fatal(err)
	}
	point := []fr.Element{fr.NewElement(2), fr.NewElement(3), fr.NewElement(7)}
	var proof bytes.Buffer
	openVector(&proof, data, 1, point)
	claim, err := authenticateVector(bytes.NewReader(proof.Bytes()), rows, point)
	expected := data.Evaluate(point, nil)
	if err != nil || !expected.Equal(&claim) {
		t.Fatalf("opening did not authenticate the multilinear evaluation: %v", err)
	}
	changed := bytes.Clone(proof.Bytes())
	changed[31] ^= 1
	if _, err := authenticateVector(bytes.NewReader(changed), rows, point); err == nil {
		t.Fatal("changed field opening accepted")
	}
	modulus := fr.Modulus().FillBytes(make([]byte, fr.Bytes))
	copy(changed, modulus)
	if _, err := authenticateVector(bytes.NewReader(changed), rows, point); err == nil {
		t.Fatal("noncanonical field opening accepted")
	}
	substituted := data.Clone()
	substituted[0].SetOne()
	other, err := commitRows(substituted, 4)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := authenticateVector(bytes.NewReader(proof.Bytes()), other, point); err == nil {
		t.Fatal("an opening authenticated under a different tensor commitment")
	}
}

func TestBatchedSignedQ18Constraints(t *testing.T) {
	const n = 8
	cs, err := frontend.Compile(fr.Modulus(), r1cs.NewBuilder, newRescaleCircuit(n, false))
	if err != nil {
		t.Fatal(err)
	}
	point := []fr.Element{fr.NewElement(3), fr.NewElement(2), fr.NewElement(7)}
	half := int64(q18Scale / 2)
	raw := []int64{half + 1, -half, half - 1, 1 - half, 0, accumulatorBound - 1, 1 - accumulatorBound, half}
	rounded := []int64{1, -1, 0, 0, 0, 1 << 34, -(1 << 34), 1}
	rawHash := hashLanes(raw, rawDomain, 53, 4, accumulatorBound)
	witness := func(output []int64) *rescaleCircuit {
		out := hashLanes(output, roundedDomain, 36, 7, roundedOffset)
		return rescaleWitness(raw, point, rawHash, out, evaluateRaw(raw, point))
	}
	solve := func(c *rescaleCircuit) error {
		full, err := frontend.NewWitness(c, fr.Modulus())
		if err != nil {
			return err
		}
		return cs.IsSolved(full)
	}
	if err := solve(witness(rounded)); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func() *rescaleCircuit{
		"remainder outside [0, 2^18)": func() *rescaleCircuit {
			wrong := append([]int64(nil), rounded...)
			wrong[0] = 0 // |raw| + 2^17 - |q|*2^18 = 2^18+1
			return witness(wrong)
		},
		"negative tie rounded towards zero": func() *rescaleCircuit {
			wrong := append([]int64(nil), rounded...)
			wrong[1] = 0
			return witness(wrong)
		},
		"modular negative magnitude": func() *rescaleCircuit {
			c := witness(rounded)
			c.Magnitude[0] = new(big.Int).Sub(fr.Modulus(), big.NewInt(half+1))
			c.Negative[0] = 1
			return c
		},
		"accumulator overflow": func() *rescaleCircuit {
			c := witness(rounded)
			c.Magnitude[5] = accumulatorBound
			return c
		},
		"false accumulator evaluation": func() *rescaleCircuit {
			c := witness(rounded)
			v := evaluateRaw(raw, point)
			one := fr.NewElement(1)
			c.Evaluation = v.Add(&v, &one)
			return c
		},
	}
	for name, test := range cases {
		t.Run(name, func(t *testing.T) {
			if err := solve(test()); err == nil {
				t.Fatal("invalid rescale statement passed")
			}
		})
	}
}
