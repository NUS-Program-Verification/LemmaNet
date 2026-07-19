Require Import ZArith.
Require Import Lia.

Lemma main :
  forall x, x + 0 = x.
Proof. intros. assert (H: forall x, x + 0 = x). { intros. assert (H: forall x, x + 0 = x). { intros; lia. } lia. } rewrite H. reflexivity. Qed.
