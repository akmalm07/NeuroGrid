// Package main adapts public tensor openings to gnark's BN254 GKR engine.
package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/bits"
	"os"
	"path/filepath"
	"time"

	"github.com/consensys/gnark-crypto/ecc/bn254/fr"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/mimc"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/polynomial"
	"github.com/consensys/gnark-crypto/fiat-shamir"
	gkr "github.com/consensys/gnark/gkr/bn254"
	"github.com/consensys/gnark/gkr/gkrapi"
)

const protocol = "neurogrid-public-q18-gkr-v1"

var jobRoot = "/job"

func jobFile(name string) string { return filepath.Join(jobRoot, name) }

type scope struct {
	Protocol, Model, Request, Prompt, Operation, KVBefore, KVAfter, Left, Right string
	Layer, Position, M, K, N, Q                                                 int
}

func digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func (s scope) validate() error {
	if s.Protocol != protocol || s.Q != 18 || s.M < 1 || s.M > 4096 || s.N < 1 || s.N > 16384 ||
		s.K < 2 || s.K > 16384 || s.Layer < 0 || s.Position < 0 || len(s.Model) == 0 || len(s.Model) > 128 ||
		len(s.Operation) == 0 || len(s.Operation) > 128 || s.M*s.K > 1<<25 || s.K*s.N > 1<<25 || s.M*s.N > 1<<22 {
		return errors.New("invalid tensor statement dimensions or scope")
	}
	for _, encoded := range []string{s.Request, s.Prompt, s.KVBefore, s.KVAfter, s.Left, s.Right} {
		raw, err := hex.DecodeString(encoded)
		if err != nil || len(raw) != 32 || hex.EncodeToString(raw) != encoded {
			return errors.New("invalid statement commitment")
		}
	}
	return nil
}

func file(name string, size int) ([]byte, error) {
	path := jobFile(name)
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() != int64(size) {
		return nil, fmt.Errorf("unexpected artifact size for %s", name)
	}
	data, err := os.ReadFile(path)
	if len(data) != size && err == nil {
		return nil, fmt.Errorf("artifact changed while reading %s", name)
	}
	return data, err
}

func loadScope() (scope, []byte, error) {
	var s scope
	f, err := os.Open(jobFile("scope.json"))
	if err != nil {
		return s, nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 4097))
	if err != nil || len(data) > 4096 {
		return s, nil, errors.New("invalid scope file size")
	}
	parser := json.NewDecoder(bytes.NewReader(data))
	parser.DisallowUnknownFields()
	if err = parser.Decode(&s); err != nil {
		return s, nil, err
	}
	if err = parser.Decode(new(any)); err != io.EOF {
		return s, nil, errors.New("trailing statement data")
	}
	return s, data, s.validate()
}

func bases(statement []byte, m, n int) (polynomial.MultiLin, polynomial.MultiLin, error) {
	rowVars, colVars := bits.Len(uint(m-1)), bits.Len(uint(n-1))
	names := make([]string, rowVars+colVars)
	for i := range names {
		names[i] = fmt.Sprintf("%s/output-eval/%d", protocol, i)
	}
	if len(names) == 0 {
		return polynomial.MultiLin{fr.NewElement(1)}, polynomial.MultiLin{fr.NewElement(1)}, nil
	}
	t := fiatshamir.NewTranscript(sha512.New(), names...)
	if err := t.Bind(names[0], statement); err != nil {
		return nil, nil, err
	}
	coordinates := make([]fr.Element, len(names))
	for i, name := range names {
		raw, err := t.ComputeChallenge(name)
		if err != nil {
			return nil, nil, err
		}
		coordinates[i].SetBytes(raw)
	}
	rows := make(polynomial.MultiLin, 1<<rowVars)
	cols := make(polynomial.MultiLin, 1<<colVars)
	rows[0].SetOne()
	cols[0].SetOne()
	rows.Eq(coordinates[:rowVars])
	cols.Eq(coordinates[rowVars:])
	return rows, cols, nil
}

// Fold over the opened tensors. Verification evaluates public multilinears and
// checks all claims returned by GKR without recomputing the matrix product.
// The signed lane and width bounds keep an exact dot product below 2^62 in
// magnitude, so a field equality cannot hide wraparound in an accepted dot.
func fold(s scope, a, b, raw []byte, statement []byte) (gkr.WireAssignment, fr.Element, error) {
	rows, cols, err := bases(statement, s.M, s.N)
	if err != nil {
		return nil, fr.Element{}, err
	}
	size := 1 << bits.Len(uint(s.K-1))
	va, vb := make(polynomial.MultiLin, size), make(polynomial.MultiLin, size)
	var term, value, want fr.Element
	for i := range s.M {
		for j := range s.K {
			lane := int32(binary.LittleEndian.Uint32(a[4*(i*s.K+j):]))
			if lane <= -(1<<24) || lane >= 1<<24 {
				return nil, want, errors.New("left lane outside the Q18 bound")
			}
			value.SetInt64(int64(lane))
			term.Mul(&value, &rows[i])
			va[j].Add(&va[j], &term)
		}
	}
	for i := range s.K {
		for j := range s.N {
			lane := int32(binary.LittleEndian.Uint32(b[4*(i*s.N+j):]))
			if lane <= -(1<<24) || lane >= 1<<24 {
				return nil, want, errors.New("right lane outside the Q18 bound")
			}
			value.SetInt64(int64(lane))
			term.Mul(&value, &cols[j])
			vb[i].Add(&vb[i], &term)
		}
	}
	for i := range s.M {
		for j := range s.N {
			lane := int64(binary.LittleEndian.Uint64(raw[8*(i*s.N+j):]))
			if lane <= -(1<<52) || lane >= 1<<52 {
				return nil, want, errors.New("accumulator outside signed Q36 bound")
			}
			value.SetInt64(lane)
			term.Mul(&value, &rows[i]).Mul(&term, &cols[j])
			want.Add(&want, &term)
		}
	}
	return gkr.WireAssignment{va, vb, make(polynomial.MultiLin, size)}, want, nil
}

func seed(statement [32]byte, product []byte) *mimc.Hasher {
	h := mimc.NewHasher()
	d := sha512.New()
	d.Write([]byte(protocol + "/gnark-gkr"))
	d.Write(statement[:])
	d.Write(product)
	var el fr.Element
	el.SetBytes(d.Sum(nil))
	h.WriteElement(el)
	return h
}

func appendField(dst []byte, el *fr.Element) []byte {
	value := el.Bytes()
	return append(dst, value[:]...)
}

func gkrProof(prove bool, assignment gkr.WireAssignment, want fr.Element, statement [32]byte) ([]byte, float64, error) {
	setup := time.Now()
	api := gkrapi.New()
	x, y := api.NewInput(), api.NewInput()
	z := api.Mul(x, y)
	api.Export(z)
	c, schedule, err := api.Compile(gkrapi.PrimeField(fr.Modulus()), gkrapi.ConsolidateNone)
	if err != nil {
		return nil, 0, err
	}
	if len(c) != len(assignment) || x != 0 || y != 1 || z != 2 {
		return nil, 0, errors.New("unexpected GKR circuit layout")
	}
	setupSeconds := time.Since(setup).Seconds()
	lanes := len(assignment[z])
	productSize := lanes * fr.Bytes
	var opened []byte
	var sum fr.Element
	if prove {
		opened = make([]byte, 0, productSize+1024)
		for i := range lanes {
			assignment[z][i].Mul(&assignment[x][i], &assignment[y][i])
			sum.Add(&sum, &assignment[z][i])
			opened = appendField(opened, &assignment[z][i])
		}
		if !sum.Equal(&want) {
			return nil, setupSeconds, errors.New("incorrect matrix accumulator")
		}
		proof, claims, err := gkr.Prove(c, schedule, assignment, seed(statement, opened))
		if err != nil {
			return nil, setupSeconds, err
		}
		if err := claims.Check(assignment); err != nil {
			return nil, setupSeconds, err
		}
		for _, element := range proof.Flatten() {
			opened = appendField(opened, element)
		}
		return opened, setupSeconds, nil
	}
	info, err := os.Stat(jobFile("gkr.proof"))
	if err != nil {
		return nil, setupSeconds, err
	}
	if info.Size() <= int64(productSize) || info.Size() > int64(productSize+8192) || info.Size()%fr.Bytes != 0 {
		return nil, setupSeconds, errors.New("invalid GKR proof length")
	}
	opened, err = file("gkr.proof", int(info.Size()))
	if err != nil {
		return nil, setupSeconds, err
	}
	for i := range lanes {
		if err := assignment[z][i].SetBytesCanonical(opened[i*fr.Bytes : (i+1)*fr.Bytes]); err != nil {
			return nil, setupSeconds, err
		}
		sum.Add(&sum, &assignment[z][i])
	}
	if !sum.Equal(&want) {
		return nil, setupSeconds, errors.New("opened products do not sum to the committed output evaluation")
	}
	encoded := opened[productSize:]
	scalars := make([]fr.Element, len(encoded)/fr.Bytes)
	for i := range scalars {
		if err := scalars[i].SetBytesCanonical(encoded[i*fr.Bytes : (i+1)*fr.Bytes]); err != nil {
			return nil, setupSeconds, err
		}
	}
	proof, err := gkr.DeserializeProof(c, schedule, scalars)
	if err != nil {
		return nil, setupSeconds, err
	}
	claims, err := gkr.Verify(c, schedule, bits.Len(uint(lanes))-1, proof, seed(statement, opened[:productSize]))
	if err != nil {
		return nil, setupSeconds, err
	}
	if len(claims[int(x)]) == 0 || len(claims[int(y)]) == 0 || len(claims[int(z)]) == 0 {
		return nil, setupSeconds, errors.New("missing required GKR evaluation claim")
	}
	return opened, setupSeconds, claims.Check(assignment)
}

func run(operation string) error {
	begin := time.Now()
	s, scopeBytes, err := loadScope()
	if err != nil {
		return err
	}
	a, err := file("left.bin", 4*s.M*s.K)
	if err != nil {
		return err
	}
	b, err := file("right.bin", 4*s.K*s.N)
	if err != nil {
		return err
	}
	raw, err := file("raw.bin", 8*s.M*s.N)
	if err != nil {
		return err
	}
	loaded := time.Now()
	if digest(a) != s.Left || digest(b) != s.Right {
		return errors.New("tensor opening does not match trusted scope")
	}
	rawDigest := sha256.Sum256(raw)
	h := sha256.New()
	h.Write([]byte(protocol + "/statement\x00"))
	h.Write(scopeBytes)
	h.Write(rawDigest[:])
	statement := [32]byte(h.Sum(nil))
	committed := time.Now()
	assignment, want, err := fold(s, a, b, raw, statement[:])
	if err != nil {
		return err
	}
	folded := time.Now()
	proof, setupSeconds, err := gkrProof(operation == "prove", assignment, want, statement)
	if err != nil {
		return err
	}
	if operation == "prove" {
		if err = os.WriteFile(jobFile("gkr.proof"), proof, 0600); err != nil {
			return err
		}
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"phase": operation, "statement": hex.EncodeToString(statement[:]), "proof_bytes": len(proof),
		"read_seconds": loaded.Sub(begin).Seconds(), "commitment_seconds": committed.Sub(loaded).Seconds(),
		"fold_seconds": folded.Sub(committed).Seconds(), "circuit_setup_seconds": setupSeconds,
		"gkr_seconds": time.Since(folded).Seconds() - setupSeconds, "seconds": time.Since(begin).Seconds(),
	})
}

func main() {
	if len(os.Args) < 2 || len(os.Args) > 3 && os.Args[1] != "compact-prove" {
		fmt.Fprintln(os.Stderr, "expected a proof operation and optional job directory")
		os.Exit(2)
	}
	if len(os.Args) >= 3 {
		jobRoot = os.Args[2]
	}
	var err error
	switch operation := os.Args[1]; operation {
	case "prove", "verify":
		err = run(operation)
	case "compact-model", "compact-issue", "compact-prove", "compact-verify", "compact-key":
		err = runCompact(operation)
		if operation == "compact-prove" && len(os.Args) > 3 {
			for _, folder := range os.Args[3:] {
				if err != nil {
					break
				}
				jobRoot = folder
				err = runCompact(operation)
			}
		}
	default:
		err = errors.New("unknown proof operation")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
