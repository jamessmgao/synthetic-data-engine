# Runner instances: Harbor task containers, plus (on runner[0]) the API and
# worker-control. Reached only through SSM (no SSH key, no inbound ports).

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runner" {
  name               = "${var.name}-runner"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "runner" {
  statement {
    sid       = "ArtifactsBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    sid       = "ArtifactsObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    sid       = "RuntimeConfig"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:parameter/sde/*"]
  }
  statement {
    sid       = "DecryptSecureStrings"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "runner" {
  name   = "${var.name}-runner"
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner.json
}

resource "aws_iam_instance_profile" "runner" {
  name = "${var.name}-runner"
  role = aws_iam_role.runner.name
}

resource "aws_instance" "runner" {
  count                       = var.runner_count
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.runner_instance_type
  subnet_id                   = aws_subnet.public[count.index % 2].id
  vpc_security_group_ids      = [aws_security_group.runner.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.runner.name

  root_block_device {
    volume_size = var.runner_disk_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # containers on the box can reach the instance role
  }

  user_data = templatefile("${path.module}/runner-init.sh.tftpl", {
    compose_version = var.compose_version
    buildx_version  = var.buildx_version
  })

  tags = {
    Name = "${var.name}-runner-${count.index}"
    role = count.index == 0 ? "control" : "runner"
  }

  lifecycle {
    # A newer AMI must not replace a running runner; recreate it deliberately.
    # associate_public_ip_address reads false while a runner is stopped (no public
    # IP then), which would otherwise force a replacement of every stopped runner.
    ignore_changes = [ami, user_data, associate_public_ip_address]
  }
}
