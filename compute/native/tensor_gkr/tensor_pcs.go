package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math/bits"
	"sync"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark-crypto/ecc/bn254"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/polynomial"
)

// This is the unblinded Hyrax row-fold opening for a public-model diagnostic.
// The proof reveals one authenticated folded row and no other tensor entries.
// Hash-to-curve and multiscalar multiplication are supplied by gnark-crypto.
var tensorGenerators sync.Map

func generators(n int) ([]bn254.G1Affine, error) {
	if p, ok := tensorGenerators.Load(n); ok {
		return p.([]bn254.G1Affine), nil
	}
	p := make([]bn254.G1Affine, n)
	var coordinates [8]byte
	binary.BigEndian.PutUint32(coordinates[:4], uint32(n))
	for i := range p {
		binary.BigEndian.PutUint32(coordinates[4:], uint32(i))
		var err error
		p[i], err = bn254.HashToG1(coordinates[:], []byte("NEUROGRID-HYRAX-BN254-V1"))
		if err != nil {
			return nil, err
		}
	}
	pinned, _ := tensorGenerators.LoadOrStore(n, p)
	return pinned.([]bn254.G1Affine), nil
}

func msm(p []bn254.G1Affine, scalars []fr.Element) (bn254.G1Affine, error) {
	var point bn254.G1Affine
	_, err := point.MultiExp(p, scalars, ecc.MultiExpConfig{NbTasks: 1})
	return point, err
}

func eqBasis(point []fr.Element) polynomial.MultiLin {
	basis := make(polynomial.MultiLin, 1<<len(point))
	basis[0].SetOne()
	basis.Eq(point)
	return basis
}

func commitRows(tensor polynomial.MultiLin, width int) ([]bn254.G1Affine, error) {
	g, err := generators(width)
	if err != nil {
		return nil, err
	}
	rows := make([]bn254.G1Affine, len(tensor)/width)
	for i := range rows {
		rows[i], err = msm(g, tensor[i*width:(i+1)*width])
		if err != nil {
			return nil, err
		}
	}
	return rows, nil
}

func encodePoints(points []bn254.G1Affine) []byte {
	result := make([]byte, 0, len(points)*bn254.SizeOfG1AffineCompressed)
	for i := range points {
		data := points[i].Bytes()
		result = append(result, data[:]...)
	}
	return result
}

func readPoints(reader *bytes.Reader, count int) ([]bn254.G1Affine, error) {
	points := make([]bn254.G1Affine, count)
	var encoded [bn254.SizeOfG1AffineCompressed]byte
	for i := range points {
		if _, err := io.ReadFull(reader, encoded[:]); err != nil {
			return nil, err
		}
		n, err := points[i].SetBytes(encoded[:])
		if err != nil || n != len(encoded) || points[i].Bytes() != encoded {
			return nil, errors.New("invalid canonical commitment point")
		}
	}
	return points, nil
}

func readFields(reader *bytes.Reader, count int) (polynomial.MultiLin, error) {
	values := make(polynomial.MultiLin, count)
	var encoded [fr.Bytes]byte
	for i := range values {
		if _, err := io.ReadFull(reader, encoded[:]); err != nil {
			return nil, err
		}
		if err := values[i].SetBytesCanonical(encoded[:]); err != nil {
			return nil, fmt.Errorf("invalid canonical opening scalar: %w", err)
		}
	}
	return values, nil
}

func foldRows(tensor polynomial.MultiLin, rowPoint []fr.Element, width int) polynomial.MultiLin {
	basis := eqBasis(rowPoint)
	fold := make(polynomial.MultiLin, width)
	var t fr.Element
	for i := range basis {
		for j := range fold {
			t.Mul(&tensor[i*width+j], &basis[i])
			fold[j].Add(&fold[j], &t)
		}
	}
	return fold
}

func authenticateFold(rows []bn254.G1Affine, rowPoint []fr.Element, fold polynomial.MultiLin) error {
	if len(rows) != 1<<len(rowPoint) || len(fold) == 0 || bits.OnesCount(uint(len(fold))) != 1 {
		return errors.New("invalid polynomial opening shape")
	}
	g, err := generators(len(fold))
	if err != nil {
		return err
	}
	expected, err := msm(rows, eqBasis(rowPoint))
	if err != nil {
		return err
	}
	actual, err := msm(g, fold)
	if err != nil {
		return err
	}
	if !actual.Equal(&expected) {
		return errors.New("invalid authenticated polynomial opening")
	}
	return nil
}

func writeFields(proof *bytes.Buffer, lanes []fr.Element) {
	for i := range lanes {
		encoded := lanes[i].Bytes()
		proof.Write(encoded[:])
	}
}

func authenticateVector(reader *bytes.Reader, rows []bn254.G1Affine, point []fr.Element) (fr.Element, error) {
	rowVars := bits.Len(uint(len(rows))) - 1
	if rowVars < 0 || rowVars > len(point) {
		return fr.Element{}, errors.New("invalid vector commitment shape")
	}
	rowPoint, colPoint := point[:rowVars], point[rowVars:]
	fold, err := readFields(reader, 1<<len(colPoint))
	if err != nil {
		return fr.Element{}, err
	}
	if err := authenticateFold(rows, rowPoint, fold); err != nil {
		return fr.Element{}, err
	}
	return fold.Evaluate(colPoint, nil), nil
}

func openVector(proof *bytes.Buffer, tensor polynomial.MultiLin, rowVars int, point []fr.Element) {
	writeFields(proof, foldRows(tensor, point[:rowVars], 1<<(len(point)-rowVars)))
}
