package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math/big"
	"math/bits"
	"os"
	"time"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/polynomial"
	nativePoseidon "github.com/consensys/gnark-crypto/ecc/bn254/fr/poseidon2"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
	circuitPoseidon "github.com/consensys/gnark/std/permutation/poseidon2"
)

const q18Scale = 1 << 18
const accumulatorBound = 1 << 52
const roundedOffset = 1 << 35
const rawDomain = 0x513336
const roundedDomain = 0x513138

type rescaleCircuit struct {
	RawHash, RoundedHash, Evaluation frontend.Variable   `gnark:",public"`
	Point                            []frontend.Variable `gnark:",public"`
	Magnitude, Negative              []frontend.Variable
}

func newRescaleCircuit(n int, publicOnly bool) *rescaleCircuit {
	circuit := &rescaleCircuit{Point: make([]frontend.Variable, bits.Len(uint(n))-1)}
	if !publicOnly {
		circuit.Magnitude = make([]frontend.Variable, n)
		circuit.Negative = make([]frontend.Variable, n)
	}
	return circuit
}

func laneTag(domain, count int) uint64 { return uint64(domain)<<32 | uint64(count) }

func (c *rescaleCircuit) Define(api frontend.API) error {
	n := len(c.Magnitude)
	poseidon, err := circuitPoseidon.NewPoseidon2(api)
	if err != nil {
		return err
	}
	var rawHash, roundedHash frontend.Variable = laneTag(rawDomain, n), laneTag(roundedDomain, n)
	var packedRaw, packedRounded frontend.Variable = 0, 0
	raw := make([]frontend.Variable, n)
	for i := range raw {
		api.AssertIsBoolean(c.Negative[i])
		magnitudeBits := api.ToBinary(c.Magnitude[i], 52)
		// Bits 0..16 are below half. Bit 17 is the exact carry from adding
		// half the divisor. The higher bits are the floor quotient.
		quotient := api.Add(api.FromBinary(magnitudeBits[18:]...), magnitudeBits[17])
		raw[i] = api.Select(c.Negative[i], api.Neg(c.Magnitude[i]), c.Magnitude[i])
		rounded := api.Select(c.Negative[i], api.Neg(quotient), quotient)
		rawWeight := new(big.Int).Lsh(big.NewInt(1), uint(53*(i%4)))
		roundedWeight := new(big.Int).Lsh(big.NewInt(1), uint(36*(i%7)))
		packedRaw = api.Add(packedRaw, api.Mul(api.Add(raw[i], accumulatorBound), rawWeight))
		packedRounded = api.Add(packedRounded, api.Mul(api.Add(rounded, roundedOffset), roundedWeight))
		if i%4 == 3 || i == n-1 {
			rawHash = poseidon.Compress(rawHash, packedRaw)
			packedRaw = 0
		}
		if i%7 == 6 || i == n-1 {
			roundedHash = poseidon.Compress(roundedHash, packedRounded)
			packedRounded = 0
		}
	}
	api.AssertIsEqual(c.RawHash, rawHash)
	api.AssertIsEqual(c.RoundedHash, roundedHash)
	for _, r := range c.Point {
		half := len(raw) / 2
		for i := range half {
			raw[i] = api.Add(raw[i], api.Mul(r, api.Sub(raw[i+half], raw[i])))
		}
		raw = raw[:half]
	}
	api.AssertIsEqual(c.Evaluation, raw[0])
	return nil
}

func hashLanes(lanes []int64, domain, laneBits, width int, offset int64) fr.Element {
	hasher := nativePoseidon.NewCompressor(nativePoseidon.NewDefaultPermutation())
	h := fr.NewElement(laneTag(domain, len(lanes)))
	var packed, value, base fr.Element
	base.SetUint64(1 << laneBits)
	for start := 0; start < len(lanes); start += width {
		packed.SetZero()
		for i := min(start+width, len(lanes)) - 1; i >= start; i-- {
			value.SetInt64(lanes[i] + offset)
			packed.Mul(&packed, &base).Add(&packed, &value)
		}
		hasher.CompressField(&h, &h, &packed)
	}
	return h
}

func roundQ18(raw int64) int64 {
	if raw < 0 {
		return -roundQ18(-raw)
	}
	return (raw + q18Scale/2) / q18Scale
}

func rescaleWitness(raw []int64, coords []fr.Element, rawHash, roundedHash, evaluation fr.Element) *rescaleCircuit {
	n := 1 << len(coords)
	c := newRescaleCircuit(n, raw == nil)
	c.RawHash, c.RoundedHash, c.Evaluation = rawHash, roundedHash, evaluation
	for i := range coords {
		c.Point[i] = coords[i]
	}
	for i, value := range raw {
		if value < 0 {
			c.Negative[i], c.Magnitude[i] = 1, -value
		} else {
			c.Negative[i], c.Magnitude[i] = 0, value
		}
	}
	return c
}

func evaluateRaw(data []int64, coords []fr.Element) fr.Element {
	lanes := make(polynomial.MultiLin, len(data))
	for i, val := range data {
		lanes[i].SetInt64(val)
	}
	return lanes.Evaluate(coords, nil)
}

func saveArtifact(name string, write func(io.Writer) (int64, error)) error {
	f, err := os.OpenFile(jobFile(name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, writeErr := write(f)
	return errors.Join(writeErr, f.Close())
}

func loadArtifact(name string, artifact io.ReaderFrom) error {
	f, err := os.Open(jobFile(name))
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := artifact.ReadFrom(f); err != nil {
		return err
	}
	if n, err := f.Read(make([]byte, 1)); n != 0 || err != io.EOF {
		return fmt.Errorf("trailing data in %s", name)
	}
	return nil
}

func setupRescale(n int) error {
	start := time.Now()
	cs, err := frontend.Compile(fr.Modulus(), r1cs.NewBuilder, newRescaleCircuit(n, false))
	if err != nil {
		return err
	}
	compileSeconds := time.Since(start).Seconds()
	start = time.Now()
	pk, vk, err := groth16.Setup(cs)
	if err != nil {
		return err
	}
	keySeconds := time.Since(start).Seconds()
	start = time.Now()
	for _, artifact := range []struct {
		name  string
		write func(io.Writer) (int64, error)
	}{{"rescale.r1cs", cs.WriteTo}, {"rescale.pk", pk.WriteRawTo}, {"rescale.vk", vk.WriteTo}} {
		if err := saveArtifact(artifact.name, artifact.write); err != nil {
			return err
		}
	}
	provingKey, err := os.ReadFile(jobFile("rescale.pk"))
	if err != nil {
		return err
	}
	if err := os.WriteFile(jobFile("rescale.pk.sha256"), []byte(digest(provingKey)), 0600); err != nil {
		return err
	}
	return reportCompact("compact_key", map[string]any{"constraints": cs.GetNbConstraints(),
		"compile_seconds": compileSeconds, "keygen_seconds": keySeconds, "serialize_seconds": time.Since(start).Seconds()})
}

func loadPinnedProvingKey(key groth16.ProvingKey) error {
	// The installation pins the locally generated key separately from proof
	// inputs. A digest match authenticates all encoded points before loading
	// them. Verification keys and untrusted proofs always use checked decoders.
	pin := os.Getenv("NEUROGRID_COMPACT_KEY_PIN")
	if pin == "" {
		return loadArtifact("rescale.pk", key)
	}
	if len(pin) != 64 {
		return errors.New("invalid trusted proving-key pin")
	}
	info, err := os.Stat(jobFile("rescale.pk"))
	if err != nil {
		return err
	}
	if info.Size() <= 0 || info.Size() > 512<<20 {
		return errors.New("invalid pinned proving key size")
	}
	data, err := file("rescale.pk", int(info.Size()))
	if err != nil {
		return err
	}
	if digest(data) != pin {
		return errors.New("proving key differs from its trusted installation pin")
	}
	reader := bytes.NewReader(data)
	if _, err := key.UnsafeReadFrom(reader); err != nil {
		return err
	}
	if reader.Len() != 0 {
		return errors.New("trailing data in pinned proving key")
	}
	return nil
}

type rescaleKeys struct {
	cs     constraint.ConstraintSystem
	pk     groth16.ProvingKey
	csInfo os.FileInfo
	pkInfo os.FileInfo
}

var loadedRescale rescaleKeys

func loadProvingState() (rescaleKeys, error) {
	csInfo, err := os.Stat(jobFile("rescale.r1cs"))
	if err != nil {
		return rescaleKeys{}, err
	}
	pkInfo, err := os.Stat(jobFile("rescale.pk"))
	if err != nil {
		return rescaleKeys{}, err
	}
	same := func(a, b os.FileInfo) bool {
		return b != nil && os.SameFile(a, b) && a.Size() == b.Size() && a.ModTime().Equal(b.ModTime())
	}
	if same(csInfo, loadedRescale.csInfo) && same(pkInfo, loadedRescale.pkInfo) {
		return loadedRescale, nil
	}
	state := rescaleKeys{groth16.NewCS(ecc.BN254), groth16.NewProvingKey(ecc.BN254), csInfo, pkInfo}
	if err := loadArtifact("rescale.r1cs", state.cs); err != nil {
		return rescaleKeys{}, err
	}
	if err := loadPinnedProvingKey(state.pk); err != nil {
		return rescaleKeys{}, err
	}
	loadedRescale = state
	return state, nil
}

func proveRescale(c *rescaleCircuit) ([]byte, float64, float64, error) {
	start := time.Now()
	state, err := loadProvingState()
	if err != nil {
		return nil, 0, 0, err
	}
	loadSeconds := time.Since(start).Seconds()
	start = time.Now()
	witness, err := frontend.NewWitness(c, fr.Modulus())
	if err != nil {
		return nil, loadSeconds, 0, err
	}
	proof, err := groth16.Prove(state.cs, state.pk, witness)
	if err != nil {
		return nil, loadSeconds, 0, err
	}
	var encoded bytes.Buffer
	if _, err := proof.WriteTo(&encoded); err != nil {
		return nil, loadSeconds, 0, err
	}
	return encoded.Bytes(), loadSeconds, time.Since(start).Seconds(), nil
}

func verifyRescale(data []byte, c *rescaleCircuit) error {
	// This circuit has no Groth16 wire commitments. Reject noncanonical
	// encodings before the upstream decoder reads a variable-length vector.
	if len(data) != 164 || binary.BigEndian.Uint32(data[128:132]) != 0 ||
		data[0]>>6 == 0 || data[32]>>6 == 0 || data[96]>>6 == 0 || data[132]>>6 == 0 {
		return errors.New("invalid batched rescale proof encoding")
	}
	proof, vk := groth16.NewProof(ecc.BN254), groth16.NewVerifyingKey(ecc.BN254)
	reader := bytes.NewReader(data)
	if _, err := proof.ReadFrom(reader); err != nil || reader.Len() != 0 {
		return errors.New("invalid batched rescale proof encoding")
	}
	if err := loadArtifact("rescale.vk", vk); err != nil {
		return err
	}
	witness, err := frontend.NewWitness(c, fr.Modulus(), frontend.PublicOnly())
	if err != nil {
		return err
	}
	if err := groth16.Verify(proof, vk, witness); err != nil {
		return fmt.Errorf("batched rescale proof rejected: %w", err)
	}
	return nil
}
